# Create your views here.
import re
import os.path

from django.http import HttpResponse, Http404
from templatetags.filertags import filerthumbnail, filerfile

def css_preprocessor(request, path):
    dirname = os.path.dirname(path)
    f = filerthumbnail(path)
    if not f and path.endswith('.css'):
        raise Http404('No such file.')

    def change_urls(match):
        relative_path = match.groups()[0]
        path = os.path.join(dirname, relative_path)
        return "url('%s')" % filerfile(path)

    return HttpResponse(re.sub(
        r"url\(['\"]([^']+)['\"]\)", change_urls, f.read()
    ))
