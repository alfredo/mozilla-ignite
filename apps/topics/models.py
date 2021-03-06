from django.conf import settings
from django.db import models

from tower import ugettext_lazy as _

from innovate.models import BaseModel


class Topic(BaseModel):
    name = models.CharField(verbose_name=_(u'Name'), max_length=100)
    slug = models.SlugField(verbose_name=_(u'Slug'), unique=True,
                            max_length=100)
    description = models.CharField(verbose_name=_(u'Description'),
                                   max_length=100)
    long_description = models.TextField(verbose_name=_(u'Long Description'),
                                        blank=True)
    image = models.ImageField(verbose_name=_(u'Image'), blank=True,
                              upload_to=settings.TOPIC_IMAGE_PATH, null=True,
                              max_length=settings.MAX_FILEPATH_LENGTH)
    draft = models.BooleanField(default=False)

    @property
    def image_url(self):
        media_url = getattr(settings, 'MEDIA_URL', '')
        path = lambda f: f and '%s%s' % (media_url, f)
        return path(self.image) or path('img/topic-default.gif')

    def __unicode__(self):
        if self.draft:
            return "%s (in draft)" % self.name
        else:
            return self.name
