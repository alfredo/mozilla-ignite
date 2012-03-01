from datetime import timedelta, datetime

from django.db.models import Q
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from challenges.models import Submission, Phase
from timeslot.models import BookingAvailability

class Command(BaseCommand):
    help = """Throttles the booking of the winning submissions,
    and email the users when they are going to be ready"""

    def handle(self, *args, **options):
        # submitions allocated
        submitions_ids = [item.id for item in BookingAvailability.objects.all()]
        phase = Phase.objects.get_ideation_phase()
        # make sure the ideation phase has finished
        if phase.end_date > datetime.utcnow():
            raise CommandError('Ideation phase is in progress '
                               'try again after %s' % phase.end_date)
        # winner entries for ideation phase that haven't been allocated
        object_list = Submission.objects.green_lit().\
            filter(~Q(id__in=submitions_ids), phase=phase).order_by('?')
        print 'Allocating %s entries' % len(object_list)
        # 3 days after the phase has finished
        allocation_date = phase.end_date + timedelta(days=3)
        date_increments = timedelta(seconds=settings.BOOKING_THROTTLING_TIMEDELTA)
        print 'Booking from: %s' % allocation_date
        for i, submission in enumerate(object_list):
            # Increase availability date for this batch when the
            # limit has been reached and if throttling is enabled
            if i and not i % settings.BOOKING_THROTTLING_USERS and \
                settings.BOOKING_THROTTLING:
                allocation_date += date_increments
            data = {
                'submission': submission,
                'available_on': allocation_date,
                }
            booking = BookingAvailability.objects.create(**data)
            print ' %s created' % booking
