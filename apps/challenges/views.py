import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.conf import settings
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Q
from django.forms.formsets import formset_factory
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.list import ListView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import ProcessFormView, UpdateView, \
                                      DeleteView, ModelFormMixin
import jingo
from tower import ugettext as _
from awards.forms import AwardForm
from awards.models import JudgeAllowance, SubmissionAward, Award
from voting.models import Vote
from badges.models import SubmissionBadge
from commons.helpers import get_page
from challenges.decorators import phase_open_required, phase_closed_required
from challenges.forms import (EntryForm, EntryLinkForm, InlineLinkFormSet,
                              JudgingForm, NewEntryForm)
from challenges.models import (Challenge, Phase, Submission, Category,
                               ExternalLink, Judgement, SubmissionParent,
                               JudgeAssignment, SubmissionVersion)
from projects.models import Project
from timeslot.models import TimeSlot

challenge_humanised = {
    'title': 'Title',
    'brief_description': 'Summary',
    'description': 'Full description',
    'sketh_note': 'Napkin sketch',
    'category': 'Category',
    'terms_and_conditions': 'Terms and conditions',
}


LOGGER = logging.getLogger(__name__)

judge_required = permission_required('challenges.judge_submission')

def is_judge(user):
    """Helper to determine if user is a Judge"""
    return user.has_perm('challenges.judge_submission')

class JingoTemplateMixin(TemplateResponseMixin):
    """View mixin to render through Jingo rather than Django's renderer."""
    
    def render_to_response(self, context, **response_kwargs):
        """Render using Jingo and return the response."""
        template_names = self.get_template_names()
        if len(template_names) > 1:
            LOGGER.info('Jingo only works with a single template name; '
                        'discarding ' + ', '.join(template_names[1:]))
        template_name = template_names[0]
        
        return jingo.render(self.request, template_name, context,
                            **response_kwargs)


def show(request, project, slug, template_name='challenges/show.html', category=False):
    """Show an individual project challenge."""
    try:
        challenge = (Challenge.objects.select_related('project')
                     .get(project__slug=project, slug=slug))
    except Challenge.DoesNotExist:
        raise Http404
    project = challenge.project
    """Pagination options """
    entry_set = Submission.objects.visible(request.user)
    entry_set = entry_set.filter(phase__challenge=challenge)
    if category:
        entry_set = entry_set.filter(category__name=category)
    paginator = Paginator(entry_set, 6)
    page = get_page(request.GET)
    try:
        entries = paginator.page(page)
    except (EmptyPage, InvalidPage):
        entries = paginator.page(paginator.num_pages)
    try:
        category = Category.objects.get(slug=category)
    except ObjectDoesNotExist:
        category = False

    return jingo.render(request, template_name, {
        'challenge': challenge,
        'project': project,
        'phases': list(enumerate(challenge.phases.all(), start=1)),
        'entries': entries,
        'categories': Category.objects.get_active_categories(),
        'category': category,
        'days_remaining': request.phase['days_remaining']
    })


def entries_all(request, project, slug):
    """Show all entries (submissions) to a challenge."""
    return show(request, project, slug, template_name='challenges/all.html')


class WinningEntriesView(ListView, JingoTemplateMixin):
    """Show entries that have been marked as winners."""
    
    template_name = 'challenges/winning.html'
    context_object_name = 'entries'
    
    def get_context_data(self, **kwargs):
        context = super(WinningEntriesView, self).get_context_data(**kwargs)
        context.update(project=self.project, challenge=self.challenge)
        return context
    
    def get_queryset(self):
        self.project = get_object_or_404(Project, slug=self.kwargs['project'])
        self.challenge = get_object_or_404(self.project.challenge_set,
                                           slug=self.kwargs['slug'])
        submissions = (Submission.objects.visible(self.request.user)
                       .filter(phase__challenge=self.challenge)
                       .filter(is_winner=True))
        return submissions


entries_winning = WinningEntriesView.as_view()


class AssignedEntriesView(ListView, JingoTemplateMixin):
    """Show entries assigned to be judged by the current user."""
    template_name = 'challenges/assigned.html'
    context_object_name = 'entries'

    def get_context_data(self, **kwargs):
        """Add green-lit entries to the context when:
        - Judge has allowance
        - The Award money has been released
        - It is the same Phase/Round that was judged
        """
        context = super(AssignedEntriesView, self).get_context_data(**kwargs)
        profile = self.request.user.get_profile()
        allowance = JudgeAllowance.objects.get_for_judge(profile)
        if not allowance:
            return context
        context['allowance'] = allowance
        # Awarded submissions
        awarded_list = (allowance.submissionaward_set
                        .filter(judge_allowance__judge=profile))
        context['awarded_list'] = awarded_list
        awarded_ids = [o.submission.id for o in awarded_list]
        # Green-lit submissions for this round awaiting to be awarded
        args = [self.phase]
        if self.phase.judging_phase_round:
            args.append(self.phase.judging_phase_round)
        context['phase'] = self.phase
        context['greenlit_list'] = (Submission.objects.green_lit(*args)
                                    .filter(~Q(id__in=awarded_ids)))
        return context

    def get_queryset(self):
        self.project = get_object_or_404(Project, slug=self.kwargs['project'])
        self.challenge = get_object_or_404(self.project.challenge_set,
                                           slug=self.kwargs['slug'])
        self.phase = Phase.objects.get_judging_phase(settings.IGNITE_CHALLENGE_SLUG)
        qs = {
            'phase': self.phase,
            'phase__challenge': self.challenge,
            'judgeassignment__judge__user': self.request.user
            }
        if self.phase.judging_phase_round:
            qs.update({'phase_round': self.phase.judging_phase_round})
        submissions = (Submission.objects.filter(**qs)
                       .select_related('judgement__judge__user', 'judgement'))
        # Add a custom attribute for whether user has judged this submission
        for submission in submissions:
            submission.has_judged = any(j.judge.user == self.request.user
                                        for j in submission.judgement_set.all())
        return sorted(submissions, key=lambda s: s.has_judged, reverse=True)


entries_assigned = judge_required(AssignedEntriesView.as_view())


class JudgedEntriesView(ListView, JingoTemplateMixin):
    """Show all entries that have been judged."""
    template_name = 'challenges/judged.html'
    context_object_name = 'entries'

    def get_queryset(self):
        self.project = get_object_or_404(Project, slug=self.kwargs['project'])
        self.challenge = get_object_or_404(self.project.challenge_set,
                                           slug=self.kwargs['slug'])
        submissions = Submission.objects.filter(judgement__isnull=False)
        submissions = submissions.distinct()
        submissions = submissions.select_related('judgement__judginganswer__criterion')
        for submission in submissions:
            judgements = [j for j in submission.judgement_set.all() if j.complete]
            total = sum(j.get_score() for j in judgements)
            if judgements:
                submission.average_score = total / len(judgements)
            else:
                submission.average_score = 0
            submission.judgement_count = len(judgements)

        submissions = sorted(submissions, key=lambda s: s.average_score,
                             reverse=True)
        return submissions


entries_judged = judge_required(JudgedEntriesView.as_view())


def entries_category(request, project, slug, category):
    """Show all entries to a specific category"""
    return show(request, project, slug, template_name='challenges/all.html', category=category)


def extract_form_errors(form, link_form):
    form_errors = {}
    # this feels horrible but I think required to create a custom error list
    for k in form.errors.keys():
        humanised_key = challenge_humanised[k]
        form_errors[humanised_key] =  form.errors[k].as_text()
    if not link_form.is_valid():
        form_errors['External links'] = "* Please provide a valid URL and name for each link provided"
    
    return form_errors


@phase_open_required(methods_allowed=['GET'])
@login_required
def create_entry(request, project, slug):
    """Creates a ``Submission`` from the user details"""
    try:
        challenge = (Challenge.objects.select_related('project')
                     .get(project__slug=project, slug=slug))
    except Challenge.DoesNotExist:
        raise Http404
    project = challenge.project
    profile = request.user.get_profile()
    LinkFormSet = formset_factory(EntryLinkForm, extra=2)
    form_errors = False
    if request.method == 'POST':
        # If there is not an active phase it shouldn't be able to get here
        phase = Phase.objects.get_current_phase(slug)
        if not phase:
            raise Http404
        form = NewEntryForm(data=request.POST,
            files=request.FILES)
        link_form = LinkFormSet(request.POST, prefix="externals")
        if form.is_valid() and link_form.is_valid():
            entry = form.save(commit=False)
            entry.created_by = profile
            entry.phase = phase
            if phase.current_round:
                entry.phase_round = phase.current_round
            entry.save()
            for link in link_form.cleaned_data:
                if all(i in link for i in ("name", "url")):
                    ExternalLink.objects.create(
                        name = link['name'],
                        url = link['url'],
                        submission = entry
                    )
            # create the ``SubmissionParent`` for this entry
            SubmissionParent.objects.create(submission=entry)
            if entry.is_draft:
                msg = _('<strong>Your entry has been saved as draft.</strong>'
                        ' When you want the world to see it then uncheck the '
                        '"Save as draft?" option from your idea editting page')
            else:
                msg = _('Your entry has been posted successfully and is now '
                        'available for public review')
            messages.success(request, msg)
            return HttpResponseRedirect(phase.challenge.get_entries_url())
        else:
            form_errors = extract_form_errors(form, link_form)
    else:
        form = NewEntryForm()
        link_form = LinkFormSet(prefix='externals')
    return jingo.render(request, 'challenges/create.html', {
        'project': project,
        'challenge': challenge,
        'form': form,
        'link_form': link_form,
        'errors': form_errors
    })


def entry_version(request, project, slug, entry_id):
    """Redirects an ``Submission`` version to the ``SubmissionParent``"""
    try:
        challenge = (Challenge.objects.select_related('project')
                     .get(project__slug=project, slug=slug))
    except Challenge.DoesNotExist:
        raise Http404
    # Submisison is on the Parent
    try:
        parent = (SubmissionParent.objects
                  .get(submission__id=entry_id,
                       submission__phase__challenge=challenge))
        return HttpResponseRedirect(parent.get_absolute_url())
    except SubmissionParent.DoesNotExist:
        pass
    # Submission was versioned and has a new SubmissionParent
    try:
        version = (SubmissionVersion.objects.select_related('submission_list')
                   .filter(submission__id=entry_id,
                           submission__phase__challenge=challenge))[0]
    except IndexError:
        raise Http404
    return HttpResponseRedirect(version.parent.get_absolute_url())


def entry_show(request, project, slug, entry_id, judging_form=None):
    """Detail of an idea, show any related information to it"""
    try:
        challenge = (Challenge.objects.select_related('project')
                     .get(project__slug=project, slug=slug))
    except Challenge.DoesNotExist:
        raise Http404
    project = challenge.project
    # SubmissionParent acts as an proxy for any of the revisions.
    # and it only shows the current revision
    try:
        parent = (SubmissionParent.objects.select_related('submission')
                  .get(slug=entry_id, submission__phase__challenge=challenge))
    except SubmissionParent.DoesNotExist:
        raise Http404
    entry = parent.submission
    if not entry.visible_to(request.user):
        raise Http404
    # Sidebar
    ## Voting
    user_vote = Vote.objects.get_for_user(parent, request.user)
    votes = Vote.objects.get_score(parent)

    ## Previous/next modules
    # We can't use Django's built-in methods here, because we need to restrict
    # to entries the current user is allowed to see
    entries = Submission.objects.visible(request.user)
    try:
        previous_entries = entries.filter(Q(created_on__lt=entry.created_on) |
                                          Q(pk__lt=entry.pk))
        previous = previous_entries.order_by('-created_on')[0]
    except IndexError:
        previous = entries.order_by('-created_on')[0]
    try:
        next_entries = entries.filter(Q(created_on__gt=entry.created_on) |
                                      Q(pk__gt=entry.pk))
        next = next_entries.order_by('created_on')[0]
    except IndexError:
        next = entries.order_by('created_on')[0]
    if request.phase['is_open'] or not entry.judgeable_by(request.user):
        judging_form = None
        judge_assigned = False
    else:
        # the ``Submission`` belongs to the current combination of
        # judging Phase and PhaseRound
        phase = Phase.objects.get_judging_phase(settings.IGNITE_CHALLENGE_SLUG)
        if any([entry.phase != phase,
                entry.phase_round != phase.judging_phase_round]):
            judging_form = None
            judge_assigned = None
        else:
            # Judging is only open if the submission has been assigned
            if judging_form is None:
                judging_form = _get_judging_form(user=request.user, entry=entry)
            assignments = JudgeAssignment.objects
            judge_assigned = assignments.filter(judge__user=request.user,
                                                submission=entry).exists()
    award_form = None
    allowance = None
    if is_judge(request.user):
        # Award form, judge has allowance for this Submission
        profile = request.user.get_profile()
        allowance = JudgeAllowance.objects.get_for_judge(profile)
        if allowance:
            award_list = (allowance.submissionaward_set
                          .filter(submission=entry,
                                  judge_allowance__judge=profile))
            if award_list:
                awarded = award_list[0]
                award_form = AwardForm({'amount': awarded.amount})
                if all([not awarded,
                        allowance.award.phase == entry.phase,
                        allowance.award.phase_round == entry.phase_round]):
                    award_form = AwardForm()
    # Use all the submission ids to sumarize any information required for the
    # project homepage
    submission_ids = list(parent.submissionversion_set.all()
                      .values_list('submission__id', flat=True)) + [entry.id]
    # Cache the awarded badges
    badge_list = (SubmissionBadge.objects.select_related('badge')
                  .filter(is_published=True, submission__in=submission_ids))
    # Cache webcast list
    webcast_list = TimeSlot.objects.filter(is_booked=True,
                                           submission__in=submission_ids)
    return jingo.render(request, 'challenges/show_entry.html', {
        'project': project,
        'challenge': challenge,
        'entry': entry,
        'links': entry.externallink_set.all() or False,
        'previous': previous or False,
        'next': next or False,
        'user_vote': user_vote,
        'votes': votes['score'],
        'excluded': entry.exclusionflag_set.exists(),
        'judging_form': judging_form,
        'judge_assigned': judge_assigned,
        'webcast_list': webcast_list,
        'badge_list': badge_list,
        'parent': parent,
        'award_form': award_form,
        'allowance': allowance,
    })


def _get_judging_form(user, entry, data=None, form_class=JudgingForm):
    try:
        judgement = Judgement.objects.get(judge=user.get_profile(),
                                          submission=entry)
        criteria = [a.criterion for a in judgement.answers.all()]
    except Judgement.DoesNotExist:
        judgement = Judgement(judge=user.get_profile(), submission=entry)
        criteria = entry.phase.judgement_criteria.all()
    
    return form_class(data, instance=judgement, criteria=criteria)


class SingleSubmissionMixin(SingleObjectMixin):
    """Mixin for views operating on a single submission.
    
    This mixin handles looking up the submission and checking user permissions.
    
    """
    
    def _get_challenge(self):
        return get_object_or_404(Challenge,
                                 project__slug=self.kwargs['project'],
                                 slug=self.kwargs['slug'])
    
    def get_queryset(self):
        return Submission.objects.filter(phase__challenge=self._get_challenge(),
                                         submissionparent__isnull=False)
    
    def get_object(self, *args, **kwargs):
        obj = super(SingleSubmissionMixin, self).get_object(*args, **kwargs)
        if not self._check_permission(obj, self.request.user):
            raise PermissionDenied()
        return obj
    
    def _check_permission(self, submission, user):
        """Check the given user is allowed to use this view.
        
        Return True if the operation is allowed; otherwise return False.
        
        Inheriting views should override this with the appropriate permission
        checks.
        
        """
        return True


class EntryJudgementView(JingoTemplateMixin, SingleSubmissionMixin, ModelFormMixin, ProcessFormView):
    
    form_class = JudgingForm
    
    @property
    def success_url(self):
        # Need to implement this as a property so it's only called after load
        return reverse('entries_assigned')
    
    def _check_permission(self, submission, user):
        """Validates the entry ``Phase`` and ``PhaseRound`` is available
        for judging and the user has privileges"""
        phase = Phase.objects.get_judging_phase(settings.IGNITE_CHALLENGE_SLUG)
        if any([submission.phase != phase,
                submission.phase_round != phase.judging_phase_round]):
            return False
        return submission.judgeable_by(user)
    
    def get_form(self, form_class):
        return _get_judging_form(data=self.request.POST, user=self.request.user,
                                 entry=self.get_object(), form_class=form_class)
    
    def get(self, request, *args, **kwargs):
        # Redirect back to the entry view
        # Strictly speaking, this view shouldn't accept GET requests, but in
        # case someone submits theform, gets errors and reloads this URL,
        # redirecting back to the entry seems the sanest choice
        return HttpResponseRedirect(self.get_object().get_absolute_url())
    
    def form_invalid(self, form):
        # Show the entry page with the form (and errors)
        return entry_show(self.request, self.kwargs['project'],
                          self.kwargs['slug'], self.kwargs['pk'],
                          judging_form=form)
    
    def form_valid(self, form):
        response = super(EntryJudgementView, self).form_valid(form)
        messages.success(self.request,
                         _('Success! Thanks for evaluating the submission.'))
        return response

    # Make sure it is only available when the phases are closed
    @method_decorator(phase_closed_required(methods_allowed=['GET']))
    def dispatch(self, *args, **kwargs):
        return super(EntryJudgementView, self).dispatch(*args, **kwargs)

entry_judge = EntryJudgementView.as_view()


class EditEntryView(UpdateView, JingoTemplateMixin, SingleSubmissionMixin):
    
    form_class = EntryForm
    link_form_class = InlineLinkFormSet
    template_name = 'challenges/edit.html'
    
    def _check_permission(self, submission, user):
        return submission.editable_by(user)
    
    # The following two methods are analogous to Django's generic form methods
    
    def get_link_form(self, link_form_class):
        return link_form_class(**self.get_link_form_kwargs())
    
    def get_link_form_kwargs(self):
        form_kwargs = super(EditEntryView, self).get_form_kwargs()
        # Initial data doesn't apply to formsets
        del form_kwargs['initial']
        form_kwargs.update(instance=self.object, prefix='externals')
        return form_kwargs
    
    def get_forms(self):
        """Return the forms available to this view as a dictionary."""
        form = self.get_form(self.get_form_class())
        link_form = self.get_link_form(self.link_form_class)
        return {'form': form, 'link_form': link_form}
    
    def get(self, request, *args, **kwargs):
        """Respond to a GET request by displaying the edit form."""
        self.object = self.get_object()
        
        context = self.get_context_data(**self.get_forms())
        """
        We now access errrors direct in the template - so with no errors 
        it throws undefined
        """
        context['errors'] = {}
        return self.render_to_response(context)
    
    def post(self, request, *args, **kwargs):
        """Handle the ``Submission`` update, only available when a phase is open
        If the forms are both valid, save them and redirect to the success URL.
        If either form is invalid, render the form with the errors displayed.
        If a different round is active:
        - Archive the old submission, add the new one with the changes
        """
        self.object = self.get_object()
        forms = self.get_forms()
        form, link_form = forms['form'], forms['link_form']
        if form.is_valid() and link_form.is_valid():
            return self.form_valid(form, link_form)
        else:
            return self.form_invalid(form, link_form)

    def form_valid(self, form, link_form):
        """Saves updates the ``Submission`` if it is on the same ``Round`` and
        ``Phase`` else archive the old submission and create a new entry"""
        messages.success(self.request, 'Your entry has been updated.')
        phase = Phase.objects.get_current_phase(settings.IGNITE_CHALLENGE_SLUG)
        object_phase = self.object.phase_round if self.object.phase_round else None
        if self.object.phase == phase and object_phase == phase.current_round:
            # No changes during a different Round and Phase update the
            # current entry
            response = super(EditEntryView, self).form_valid(form)
            link_form.save()
        else:
            #  Archive the old submission and create a new entry for the current
            # Phase / Round with the new details
            previous_submission = self.object
            profile = self.request.user.get_profile()
            parent = previous_submission.parent
            submission = Submission(created_by=profile,
                                    category=previous_submission.category,
                                    phase=phase)
            if phase.current_round:
                submission.phase_round = phase.current_round
            # Pass the form data to the new submission
            kwargs = self.get_form_kwargs()
            kwargs['instance'] = submission
            new_form = self.form_class(**kwargs)
            if new_form.is_valid():
                new_submission = new_form.save()
                if not kwargs['files']:
                    new_submission.sketh_note = previous_submission.sketh_note
                    new_submission.save()
                parent.update_version(new_submission)
                # Duplicate links
                for link in link_form.cleaned_data:
                    if not link:
                        continue
                    ExternalLink.objects.create(url=link['url'],
                                                name=link['name'],
                                                submission=new_submission)
            else:
                # This is the best way to handle invalid details
                # from the form when duplicating the submission
                return self.form_invalid(new_form, link_form)
            response = HttpResponseRedirect(self.get_success_url())
        return response

    def form_invalid(self, form, link_form):
        """Display the form with errors."""
        form_errors = extract_form_errors(form, link_form)
        context = self.get_context_data(form=form, link_form=link_form,
                                        errors=form_errors)
        return self.render_to_response(context)
    
    def get_context_data(self, **kwargs):
        context = super(EditEntryView, self).get_context_data(**kwargs)
        context['challenge'] = self._get_challenge()
        context['project'] = context['challenge'].project
        return context

    @method_decorator(login_required)
    @method_decorator(phase_open_required(methods_allowed=['GET']))
    def dispatch(self, *args, **kwargs):
        return super(EditEntryView, self).dispatch(*args, **kwargs)


# This view is only available when there is a Phase open
entry_edit = EditEntryView.as_view()


class DeleteEntryView(DeleteView, JingoTemplateMixin, SingleSubmissionMixin):
    
    template_name = 'challenges/delete.html'
    success_url = '/'
    
    def _check_permission(self, submission, user):
        return submission.deletable_by(user)
    
    def get_context_data(self, **kwargs):
        context = super(DeleteEntryView, self).get_context_data(**kwargs)
        context['challenge'] = self._get_challenge()
        context['project'] = context['challenge'].project
        return context
    
    def delete(self, request, *args, **kwargs):
        # Unfortunately, we can't sensibly hook into the superclass version of
        # this method and still get things happening in the right order. We
        # would have to record the success message *before* deleting the entry,
        # which is just asking for trouble.
        self.object = self.get_object()
        # Remove all the versioned content from the Parent, since
        # the User don't want to keep any versions of this ``Submission``
        for parent in self.object.submissionparent_set.all():
            [s.submission.delete() for s in parent.submissionversion_set.all()]
        self.object.delete()
        messages.success(request, "Your submission has been deleted.")
        return HttpResponseRedirect(self.get_success_url())

    @method_decorator(login_required)
    @method_decorator(phase_open_required(methods_allowed=['GET']))
    def dispatch(self, *args, **kwargs):
        return super(DeleteEntryView, self).dispatch(*args, **kwargs)

entry_delete = DeleteEntryView.as_view()
