from datetime import datetime
from dateutil.relativedelta import relativedelta
from decimal import Decimal
from markdown import markdown

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse, NoReverseMatch
from django.core.validators import MaxLengthValidator
from django.template.defaultfilters import slugify
from django.db import models
from django.db.models import signals, Q
from django.dispatch import receiver

from challenges.lib import cached_bleach, cached_property
from django_extensions.db.fields import (AutoSlugField,
                                         CreationDateTimeField,
                                         ModificationDateTimeField)
from innovate.models import BaseModel, BaseModelManager
from projects.models import Project
from tower import ugettext_lazy as _
from users.models import Profile


class ChallengeManager(BaseModelManager):
    
    def get_by_natural_key(self, slug):
        return self.get(slug=slug)


class Challenge(BaseModel):
    """A user participation challenge on a specific project."""
    
    objects = ChallengeManager()
    
    title = models.CharField(verbose_name=_(u'Title'), max_length=60, unique=True)
    slug = models.SlugField(verbose_name=_(u'Slug'), max_length=60, unique=True)
    summary = models.TextField(verbose_name=_(u'Summary'),
                               validators=[MaxLengthValidator(200)])
    description = models.TextField(verbose_name=_(u'Description'))
    
    def natural_key(self):
        return (self.slug,)
    
    @property
    def description_html(self):
        """Challenge description with bleached HTML."""
        return cached_bleach(self.description)
    
    image = models.ImageField(verbose_name=_(u'Project image'),
                              null=True, blank=True,
                              upload_to=settings.CHALLENGE_IMAGE_PATH)
    start_date = models.DateTimeField(verbose_name=_(u'Start date'),
                                      default=datetime.utcnow)
    end_date = models.DateTimeField(verbose_name=_(u'End date'))
    moderate = models.BooleanField(verbose_name=_(u'Moderate entries'),
                                   default=False)
    allow_voting = models.BooleanField(verbose_name=_(u'Can users vote on submissions?'),
                                       default=False)
    project = models.ForeignKey(Project, verbose_name=_(u'Project'),
                                limit_choices_to={'allow_participation': True})

    def get_image_src(self):
        media_url = getattr(settings, 'MEDIA_URL', '')
        path = lambda f: f and '%s%s' % (media_url, f)
        return path(self.image) or path('img/project-default.gif')

    def __unicode__(self):
        return self.title
    
    def _lookup_url(self, view_name, kwargs=None):
        """Look up a URL related to this challenge.
        
        Note that this needs to account both for an Ignite-style URL structure,
        where there is a single challenge for the entire site, and sites where
        there are multiple challenges.
        
        """
        if kwargs is None:
            kwargs = {}
        try:
            return reverse(view_name, kwargs=kwargs)
        except NoReverseMatch:
            kwargs.update({'project': self.project.slug, 'slug': self.slug})
            return reverse(view_name, kwargs=kwargs)
    
    def get_absolute_url(self):
        """Return this challenge's URL."""
        return self._lookup_url('challenge_show')
    
    def get_entries_url(self):
        """Return the URL for this challenge's entry list."""
        return self._lookup_url('entries_all')


class PhaseManager(BaseModelManager):

    def get_from_natural_key(self, challenge_slug, phase_name):
        return self.get(challenge__slug=challenge_slug, name=phase_name)

    def get_current_phase(self, slug):
        now = datetime.utcnow()
        try:
            return self.filter(challenge__slug=slug,
                               start_date__lte=now,
                               end_date__gte=now)[0]
        except IndexError:
            return None

    def get_judging_phase(self, slug):
        """Determines the judging ``Phase`` if the end_date is in the past
        or a ``PhaseRound`` has past"""
        now = datetime.utcnow()
        try:
            return self.filter((Q(end_date__lte=now) |
                                Q(phaseround__end_date__lte=now)),
                                challenge__slug=slug).order_by('-end_date')[0]
        except IndexError:
            return None

    def get_ideation_phase(self):
        """Returns the ``Ideation`` phase"""
        try:
            return self.get(challenge__slug=settings.IGNITE_CHALLENGE_SLUG,
                            name=settings.IGNITE_IDEATION_NAME)
        except self.model.DoesNotExist:
            return None

    def get_development_phase(self):
        """Returns the ``Development`` phase"""
        try:
            return self.get(challenge__slug=settings.IGNITE_CHALLENGE_SLUG,
                            name=settings.IGNITE_DEVELOPMENT_NAME)
        except self.model.DoesNotExist:
            return None

def in_six_months():
    return datetime.utcnow() + relativedelta(months=6)


def has_phase_finished(phase):
    """Helper to determine if the ``Ideation`` phase has finished"""
    cache_key = '%s_END_DATE' % phase.upper()
    end_date = cache.get(cache_key)
    if not end_date:
        phase = Phase.objects.get_ideation_phase()
        cache.set(cache_key, end_date)
        if not phase:
            return False
        end_date = phase.end_date
    return datetime.utcnow() > end_date


class Phase(BaseModel):
    """A phase of a challenge."""
    challenge = models.ForeignKey(Challenge, related_name='phases')
    name = models.CharField(max_length=100)
    start_date = models.DateTimeField(verbose_name=_(u'Start date'),
                                      default=datetime.utcnow)
    end_date = models.DateTimeField(verbose_name=_(u'End date'),
                                    default=in_six_months)
    order = models.IntegerField()

    # managers
    objects = PhaseManager()

    class Meta:
        unique_together = (('challenge', 'name'),)
        ordering = ('order',)

    def __unicode__(self):
        return '%s (%s)' % (self.name, self.challenge.title)

    def natural_key(self):
        return self.challenge.natural_key() +  (self.name,)
    natural_key.dependencies = ['challenges.challenge']

    @cached_property
    def days_remaining(self):
        return self.end_date - datetime.utcnow()

    @cached_property
    def phase_rounds(self):
        return self.phaseround_set.all()

    @cached_property
    def current_round(self):
        """Determines the current round for this ``Phase``"""
        now = datetime.utcnow()
        for item in self.phase_rounds:
            if item.start_date <= now and item.end_date >= now:
                return item
        return None

    @cached_property
    def judging_phase_round(self):
        """Returns the ``PhaseRound`` that is being judged at the moment
        - The round should have finished
        """
        now = datetime.utcnow()
        # wed don't know if the phase_rounds are ordered by end date
        sorted_rounds = sorted(self.phase_rounds, key=lambda x: x.end_date)
        sorted_rounds.reverse()
        for phase_round in sorted_rounds:
            if phase_round.end_date <= now:
                return phase_round
        return None

@receiver(signals.post_save, sender=Phase)
def phase_update_cache(instance, **kwargs):
    """Updates the ``end_date`` on the cache for this phase"""
    key = '%s_end_date' % slugify(instance.name)
    cache.set(key.upper(), instance.end_date)


class ExternalLink(BaseModel):
    name = models.CharField(verbose_name=_(u'Link Name'),
        max_length=50)
    url = models.URLField(verbose_name=_(u'URL'),
        max_length=255, verify_exists=False)
    submission = models.ForeignKey('challenges.Submission',
        blank=True, null=True)

    def __unicode__(self):
        return u"%s -> %s" % (self.name, self.url)

class CategoryManager(BaseModelManager):
    
    def get_active_categories(self):
        
        filtered_cats = []
        for cat in Category.objects.all():
            cat_submissions = cat.submission_set.all()
            if cat_submissions.count():
                filtered_cats.append(cat)

        if len(filtered_cats) == 0:
            return False
        else:
            return filtered_cats


class Category(BaseModel):
    
    objects = CategoryManager()
    
    name = models.CharField(verbose_name=_(u'Name'), max_length=60, unique=True)
    slug = models.SlugField(verbose_name=_(u'Slug'), max_length=60, unique=True)

    def __unicode__(self):
        return self.name
    
    class Meta:
        
        verbose_name_plural = 'Categories'


class SubmissionManager(BaseModelManager):

    def eligible(self, phase, phase_round=None):
        """Return all eligible submissions
        - Not excluded
        - With an entry in the current phase.
        - Submission must be in the current ``SubmissionParent``
        - Is not a draft
        Older submissions when edited will always have an entry in the current
        phase"""
        qs = {'phase': phase}
        if phase_round:
            qs.update({'phase_round': phase_round})
        return self.filter(exclusionflag__isnull=True, is_draft=False,
                           submissionparent__status=SubmissionParent.ACTIVE,
                           **qs)

    # Note: normally anything mutable wouldn't go into a default, but we can be
    # sure this method doesn't modify the anonymous user
    def visible(self, user=AnonymousUser()):
        """Return all submissions that are visible.
        If a user is provided, return all submissions visible to that user; if
        not, return all submissions visible to the general public."""
        if user.is_superuser:
            return self.current()
        criteria = models.Q(is_draft=False)
        if not user.is_anonymous():
            criteria |= models.Q(created_by__user=user)
        # Return only active submissions
        return self.current().filter(criteria)

    def green_lit(self, phase, phase_round=None):
        """Returns all the ``Submissions`` that have been green-lit.
        Each ``Submission`` belongs to a ``Phase`` or ``PhaseRound``
        hence once it is marked as winner is green-lit for this phase"""
        return self.eligible(phase, phase_round).filter(is_winner=True)

    def current(self):
        """Returns all the ``Submissions`` that are active"""
        return self.filter(submissionparent__status=SubmissionParent.ACTIVE)


class Submission(BaseModel):
    """A user's entry into a challenge."""
    
    objects = SubmissionManager()
    
    title = models.CharField(verbose_name=_(u'Title'), max_length=60)
    brief_description = models.CharField(
        max_length=200,
        verbose_name=_(u'Brief Description'),
        help_text = _(u'Think of this as an elevator pitch - keep it short and sweet'))
    description = models.TextField(verbose_name=_(u'Description'))
    sketh_note = models.ImageField(
        verbose_name=_(u'Featured image'),
        blank=True,
        null=True,
        help_text=_(u"This will be used in our summary and list views. You "
                    u"can add more images in your description or link out to "
                    u"sets or images out on the web by adding in an external link"),
        upload_to=settings.CHALLENGE_IMAGE_PATH)
    
    category = models.ForeignKey(Category)

    @property
    def description_html(self):
        """Challenge description with bleached HTML."""
        return cached_bleach(markdown(self.description))
    
    created_by = models.ForeignKey(Profile)
    created_on = models.DateTimeField(default=datetime.utcnow)
    updated_on = ModificationDateTimeField()
    is_winner = models.BooleanField(verbose_name=_(u'A winning entry?'),
                                    default=False,
                                    help_text=_(u'Mark this entry as green lit'))
    is_draft = models.BooleanField(verbose_name=_(u'Draft?'),
        help_text=_(u"If you would like some extra time to polish your submission before making it publically then you can set it as draft. When you're ready just un-tick and it will go live"))
    phase = models.ForeignKey('challenges.Phase')
    phase_round = models.ForeignKey('challenges.PhaseRound',
                                    blank=True, null=True,
                                    on_delete=models.SET_NULL)

    @property
    def challenge(self):
        return self.phase.challenge
    
    def get_image_src(self):
        media_url = getattr(settings, 'MEDIA_URL', '')
        path = lambda f: f and '%s%s' % (media_url, f)
        return path(self.sketh_note) or path('img/project-default.gif')
    
    def __unicode__(self):
        return self.title
    
    def _lookup_url(self, view_name, kwargs=None):
        """Look up a URL related to this submission.
        
        Note that this needs to account both for an Ignite-style URL structure,
        where there is a single challenge for the entire site, and sites where
        there are multiple challenges.
        
        """
        if kwargs is None:
            kwargs = {}
        try:
            return reverse(view_name, kwargs=kwargs)
        except NoReverseMatch:
            kwargs.update({'project': self.challenge.project.slug,
                           'slug': self.challenge.slug})
            return reverse(view_name, kwargs=kwargs)

    @cached_property
    def parent(self):
        parent_list = self.submissionparent_set.all()
        if parent_list:
            return parent_list[0]
        return None

    @cached_property
    def parent_slug(self):
        if self.parent:
            return self.parent.slug
        # Fallback to the versioning list, this query is expensive.
        # Provided for consistency
        version_list = self.submissionversion_set.select_related('parent').all()
        if version_list:
            return version_list[0].parent.slug
        return None

    def get_absolute_url(self):
        """Return this submission's URL."""
        return self._lookup_url('entry_show', {'entry_id': self.parent_slug})

    def get_version_url(self):
        """Return this submission's URL."""
        return self._lookup_url('entry_version', {'entry_id': self.id})

    def get_edit_url(self):
        """Return the URL to edit this submission."""
        return self._lookup_url('entry_edit', {'pk': self.id})
    
    def get_delete_url(self):
        """Return the URL to delete this submission."""
        return self._lookup_url('entry_delete', {'pk': self.id})
    
    def get_judging_url(self):
        """Return the URL to judge this submission."""
        return self._lookup_url('entry_judge', {'pk': self.id})
    
    # Permission shortcuts, for use in templates
    
    def _permission_check(self, user, permission_name):
        """Check whether a user has a given permission on this object.
        
        This has to check both the general object and specific object cases,
        because Django doesn't do the intelligent thing here and fall back on
        the general case when a backend doesn't support per-object permissions.
        
        """
        return any(user.has_perm(permission_name, obj=obj)
                   for obj in [None, self])
    
    def visible_to(self, user):
        """Return True if the user provided can see this entry."""
        return self._permission_check(user, 'challenges.view_submission')
    
    def editable_by(self, user):
        """Return True if the user provided can edit this entry."""
        return self._permission_check(user, 'challenges.edit_submission')
    
    def deletable_by(self, user):
        """Return True if the user provided can delete this entry."""
        return self._permission_check(user, 'challenges.delete_submission')
    
    def judgeable_by(self, user):
        """Return True if the user provided is allowed to judge this entry."""
        return self._permission_check(user, 'challenges.judge_submission')
    
    def owned_by(self, user):
        """Return True if user provided owns this entry."""
        return user == self.created_by.user

    @cached_property
    def needs_booking(self):
        """Determines if this entry needs to book a Timeslot.
        - Entry has been gren lit
        - Ideation phase has finished
        - Development phase has been enabled
        - User hasn't booked a timeslot
        """
        return all([
            self.is_winner,
            has_phase_finished(settings.IGNITE_IDEATION_NAME),
            settings.DEVELOPMENT_PHASE,
            not self.timeslot_set.all(),
            ])

    @property
    def is_green_lit(self):
        return self.is_winner

    class Meta:
        ordering = ['-id']


class ExclusionFlag(models.Model):
    """Flags to exclude a submission from judging."""
    
    submission = models.ForeignKey(Submission)
    
    notes = models.TextField(blank=True)
    
    def __unicode__(self):
        return unicode(self.submission)


class JudgingCriterion(models.Model):
    """A numeric rating criterion for judging submissions."""
    
    question = models.CharField(max_length=250, unique=True)
    min_value = 0
    max_value = models.IntegerField(default=10)
    
    phases = models.ManyToManyField(Phase, blank=True,
                                    related_name='judgement_criteria',
                                    through='PhaseCriterion')
    
    def __unicode__(self):
        return self.question
    
    def clean(self):
        if self.min_value >= self.max_value:
            raise ValidationError('Invalid value range %d..%d' %
                                  (self.min_value, self.max_value))
    
    @property
    def range(self):
        """Return the valid range of values for this criterion."""
        return xrange(self.min_value, self.max_value + 1)
    
    class Meta:
        
        verbose_name_plural = 'Judging criteria'
        ordering = ('id',)


class PhaseCriterion(models.Model):
    """Assignment of judging criteria to individual phases.
    
    These include a total weight assigned to each criterion. The score from
    each criterion is multiplied up to have this weight as a maximum value.
    
    """
    
    phase = models.ForeignKey(Phase)
    criterion = models.ForeignKey(JudgingCriterion)
    
    # The total weight afforded to this criterion
    weight = models.DecimalField(max_digits=4, decimal_places=2, default=10)
    
    class Meta:
        
        unique_together = (('phase', 'criterion'),)
        verbose_name_plural = 'phase criteria'
    
    def __unicode__(self):
        return ' - '.join(map(unicode, [self.phase, self.criterion]))


class Judgement(models.Model):
    """A judge's rating of a submission."""
    
    class Incomplete(RuntimeError):
        """Error class when calculating scores on incomplete judgements."""
        
        def __init__(self, judgement):
            super_init = super(Judgement.Incomplete, self).__init__
            super_init('Judgement is incomplete', judgement)
    
    submission = models.ForeignKey(Submission)
    judge = models.ForeignKey(Profile)
    
    # answers comes through in a foreign key from JudgingAnswer
    notes = models.TextField(blank=True)
    
    def __unicode__(self):
        return ' - '.join([unicode(self.submission), unicode(self.judge)])
    
    def get_absolute_url(self):
        return self.submission.get_absolute_url()
    
    @property
    def complete(self):
        """Whether all the criteria in the submission's phase are rated."""
        criteria = JudgingCriterion.objects.filter(judginganswer__judgement=self)
        return all(c in criteria for c in
                   self.submission.phase.judgement_criteria.all())
    
    def get_score(self):
        total_score = Decimal('0')
        phase_criteria = self.submission.phase.phasecriterion_set.all()
        try:
            for pc in self.submission.phase.phasecriterion_set.all():
                answer = self.answers.get(criterion=pc.criterion)
                weighting_factor = pc.weight / pc.criterion.max_value
                total_score += weighting_factor * answer.rating
        except JudgingAnswer.DoesNotExist:
            raise Judgement.Incomplete(self)
        return total_score
    
    class Meta:
        unique_together = (('submission', 'judge'),)


class JudgingAnswer(models.Model):
    """A judge's answer to an individual judging criterion."""
    
    judgement = models.ForeignKey(Judgement, related_name='answers')
    criterion = models.ForeignKey(JudgingCriterion)
    rating = models.IntegerField()
    
    def __unicode__(self):
        return ' - '.join([unicode(self.judgement), unicode(self.criterion)])
    
    class Meta:
        unique_together = (('judgement', 'criterion'),)
    
    def clean(self):
        criterion = self.criterion
        if self.rating not in self.criterion.range:
            raise ValidationError('Rating %d is outside the range %d to %d' %
                                  (self.rating, self.criterion.min_value,
                                   self.criterion.max_value))


class JudgeAssignment(models.Model):
    """An assignment of a specific judge to a submission."""
    
    submission = models.ForeignKey(Submission)
    judge = models.ForeignKey(Profile)
    
    created_on = models.DateTimeField(default=datetime.utcnow)
    
    def __unicode__(self):
        return unicode(self.submission)
    
    class Meta:
        unique_together = (('submission', 'judge'),)


@receiver(signals.post_save, sender=JudgeAssignment)
@receiver(signals.post_save, sender=Judgement)
def judgement_flush_cache(instance, **kwargs):
    """Flush the cache for any submissions related to this instance."""
    Submission.objects.invalidate(instance.submission)


class PhaseRound(models.Model):
    """Rounds for a given ``Phase``"""
    name = models.CharField(max_length=255)
    slug = AutoSlugField(populate_from='name')
    phase = models.ForeignKey('challenges.Phase')
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()

    def __unicode__(self):
        return u'%s: %s' % (self.phase, self.name)

    @cached_property
    def is_active(self):
        now = datetime.utcnow()
        return all([now > self.start_date, now < self.end_date])

    @cached_property
    def days_remaining(self):
        return self.end_date - datetime.utcnow()


class SubmissionParent(models.Model):
    """Acts as a proxy for the ``Submissions`` so we can keep them versioned
    Slug should never change since it is the main ID for this submission
    through all the process"""
    ACTIVE = 1
    INACTIVE = 2
    REMOVED = 3
    STATUS_CHOICES = (
        (ACTIVE, _('Active')),
        (INACTIVE, _('Inactive')),
        (REMOVED, _('Removed')),
        )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    created = CreationDateTimeField()
    modified = ModificationDateTimeField()
    is_featured = models.BooleanField(default=False)
    submission = models.ForeignKey('challenges.Submission')
    status = models.IntegerField(choices=STATUS_CHOICES, default=ACTIVE)

    class Meta:
        ordering = ('-created',)

    def __unicode__(self):
        return u'Project: %s' % self.name

    def save(self, *args, **kwargs):
        """Set slug and name from the ``Submission``"""
        if not self.slug:
            self.slug = self.submission.id
        if not self.name:
            self.name = self.submission.title
        super(SubmissionParent, self).save(*args, **kwargs)

    @models.permalink
    def get_absolute_url(self):
        return ('entry_show', [self.slug])

    def update_version(self, new_submission):
        """Updates the current ``SubmissionParent`` version"""
        if not self.id:
            raise ValueError('You need to save the SubmissionParent before '
                             'updating versions')
        # Archive the current submission
        SubmissionVersion.objects.create(submission=self.submission,
                                         parent=self)
        self.name = new_submission.title
        self.submission = new_submission
        self.save()


class SubmissionVersion(models.Model):
    """Keeps track of the version of a given ``Submission``"""
    submission = models.ForeignKey('challenges.Submission')
    parent = models.ForeignKey('challenges.SubmissionParent')
    created = CreationDateTimeField()

    class Meta:
        unique_together = (('submission', 'parent'),)

    def __unicode__(self):
        return u'Version for %s on %s' % (self.submission, self.created)
