from collections import defaultdict
from datetime import datetime, timedelta

from django.contrib.auth.models import User, Group, Permission
from test_utils import TestCase
from django.test.client import RequestFactory
from django.core.cache import cache
from django.conf import settings

from challenges.tests.test_judging import judging_setup
from challenges.judging import (get_judge_profiles, get_submissions,
                                get_assignments)
from challenges.models import (Submission, Judgement, JudgeAssignment,
                               ExclusionFlag, Phase)
from ignite.tests.decorators import ignite_only
from nose.tools import eq_, ok_


def assignment_setup():
    judging_setup(create_criteria=False, submission_count=5)
    judging_permission = Permission.objects.get(codename='judge_submission')
    charlie = User.objects.get(username='charlie')
    charlie.user_permissions.add(judging_permission)
    cache.clear()


class AssignmentTest(TestCase):

    def setUp(self):
        assignment_setup()
        self.phase = Phase.objects.get()
        self.judging_permission = Permission.objects.get(
                                      codename='judge_submission')

    def assertEvenAssignment(self, assignments):
        counts = defaultdict(lambda: 0)
        for assignment in assignments:
            counts[assignment.judge] += 1
        # No judge has more than one submission more than any other
        self.assertTrue(max(counts.values()) - min(counts.values()) <= 1)

    def test_get_judges(self):
        self.assertEqual(set([j.user.username for j in get_judge_profiles()]),
                         set(['alex', 'charlie']))

    def test_get_judges_superuser(self):
        """Test superusers aren't automatically returned as judges."""
        User.objects.filter(username='bob').update(is_superuser=True)
        self.assertEqual(set([j.user.username for j in get_judge_profiles()]),
                         set(['alex', 'charlie']))
    
    def test_judge_group(self):
        """Test that judges with permission through a group are included."""
        judges = Group.objects.create(name='Judges')
        judges.permissions.add(self.judging_permission)
        for user in User.objects.filter(username__in=['alex', 'charlie']):
            user.user_permissions.clear()
            if user.username == 'charlie':
                user.groups.add(judges)
        self.assertEqual(set([j.user.username for j in get_judge_profiles()]),
                         set(['charlie']))
    
    def test_submissions(self):
        self.assertEqual(len(get_submissions(self.phase)), 5)
    
    def test_no_excluded_submissions(self):
        excluded = Submission.objects.get(title='Submission 3')
        ExclusionFlag.objects.create(submission=excluded)
        
        self.assertEqual(set(s.title for s in get_submissions(self.phase)),
                         set(['Submission %d' % n for n in [1, 2, 4, 5]]))
    
    def test_no_judged_submissions(self):
        """Test judged submissions aren't included for assignment."""
        alex_profile = User.objects.get(username='alex').get_profile()
        submission = Submission.objects.get(title='Submission 3')
        Judgement.objects.create(judge=alex_profile, submission=submission,
                                 notes='Blah')
        self.assertEqual(set(s.title for s in get_submissions(self.phase)),
                         set(['Submission %d' % n for n in [1, 2, 4, 5]]))
    
    def test_no_assigned_submissions(self):
        """Test assigned submissions aren't included for assignment."""
        alex_profile = User.objects.get(username='alex').get_profile()
        submission = Submission.objects.get(title='Submission 2')
        JudgeAssignment.objects.create(submission=submission,
                                       judge=alex_profile)
        self.assertEqual(set(s.title for s in get_submissions(self.phase)),
                         set(['Submission %d' % n for n in [1, 3, 4, 5]]))
    
    def test_no_assigned_judged_submissions(self):
        """Test previously-assigned, judged submissions."""
        alex_profile = User.objects.get(username='alex').get_profile()
        submission_a = Submission.objects.get(title='Submission 4')
        submission_b = Submission.objects.get(title='Submission 2')
        Judgement.objects.create(judge=alex_profile, submission=submission_a,
                                 notes='Blah')
        JudgeAssignment.objects.create(submission=submission_b,
                                       judge=alex_profile)
        self.assertEqual(set(s.title for s in get_submissions(self.phase)),
                         set(['Submission %d' % n for n in [1, 3, 5]]))
    
    def test_assignments(self):
        assignments = get_assignments(submissions=get_submissions(self.phase),
                                      judge_profiles=get_judge_profiles(),
                                      commit=False, judges_per_submission=2)
        self.assertEvenAssignment(assignments)
        # Check nothing has been saved
        self.assertEqual(JudgeAssignment.objects.count(), 0)
    
    def test_assignment_commit(self):
        assignments = get_assignments(submissions=get_submissions(self.phase),
                                      judge_profiles=get_judge_profiles(),
                                      commit=True, judges_per_submission=2)
        self.assertEvenAssignment(assignments)
        expected_submissions = (settings.JUDGES_PER_SUBMISSION *
                                Submission.objects.count())
        self.assertEqual(JudgeAssignment.objects.count(),
                         expected_submissions)


class AssignmentContextTest(TestCase):
    
    def setUp(self):
        assignment_setup()
        # Make sure the phase is closed
        self.phase = Phase.objects.get()
        date = datetime.utcnow() - timedelta(hours=1)
        self.phase.start_date = date
        self.phase.end_date = date
        self.phase.judging_start_date = date
        self.phase.judging_end_date = date + timedelta(days=1)
        self.phase.save()
        self.judge_profile = User.objects.get(username='alex').get_profile()

    def open_phase(self):
        date = datetime.utcnow() + timedelta(hours=1)
        self.phase.start_date = datetime.utcnow() - timedelta(hours=1)
        self.phase.end_date = date
        self.phase.judging_start_date = date + timedelta(days=1)
        self.phase.judging_end_date = date + timedelta(days=2)
        self.phase.save()
    
    @ignite_only
    def test_anonymous_context(self):
        response = self.client.get('/')
        assert response.context.get('assignment_count') is None
    
    @ignite_only
    def test_judge_context(self):
        assert self.client.login(username='alex', password='alex')
        response = self.client.get('/')
        assert response.context.get('assignment_count') is 0
    
    @ignite_only
    def test_assigned_submission(self):
        for submission in Submission.objects.all()[:2]:
            JudgeAssignment.objects.create(submission=submission,
                                           judge=self.judge_profile)
        assert self.client.login(username='alex', password='alex')
        response = self.client.get('/')
        self.assertEqual(response.context.get('assignment_count'), 2)

    @ignite_only
    def test_assigned_phase_open(self):
        self.open_phase()
        for submission in Submission.objects.all()[:2]:
            JudgeAssignment.objects.create(submission=submission,
                                           judge=self.judge_profile)
        assert self.client.login(username='alex', password='alex')
        response = self.client.get('/')
        eq_(response.context.get('assignment_count'), 0)

    @ignite_only
    def test_assigned_and_judged(self):
        """Check that submissions don't count if the judge has judged them."""
        assigned, judged = Submission.objects.all()[:2]
        for submission in [assigned, judged]:
            JudgeAssignment.objects.create(submission=submission,
                                           judge=self.judge_profile)
        Judgement.objects.create(submission=submission,
                                 judge=self.judge_profile,
                                 notes='Blah blah blah notes')
        assert self.client.login(username='alex', password='alex')
        response = self.client.get('/')
        self.assertEqual(response.context.get('assignment_count'), 1)
    
    @ignite_only
    def test_assigned_and_judged_by_other(self):
        """Check that other judges' judgements don't discount the entry."""
        assigned, judged = Submission.objects.all()[:2]
        other_judge = User.objects.get(username='bob').get_profile()
        for submission in [assigned, judged]:
            JudgeAssignment.objects.create(submission=submission,
                                           judge=self.judge_profile)
        Judgement.objects.create(submission=submission,
                                 judge=other_judge,
                                 notes='Blah blah blah notes')
        assert self.client.login(username='alex', password='alex')
        response = self.client.get('/')
        self.assertEqual(response.context.get('assignment_count'), 2)
