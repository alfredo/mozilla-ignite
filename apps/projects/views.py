from django.shortcuts import get_object_or_404

import jingo

from projects.models import Project


def all(request):
    projects = Project.objects.all()
    return jingo.render(request, 'projects/all.html', {
        'projects': projects,
    })


def show(request, slug):
    project = get_object_or_404(Project, slug=slug)
    topic = request.session.get('topic', None) or project.topics.all()[0].name
    return jingo.render(request, 'projects/show.html', {
        'project': project,
        'topic': topic
    })
