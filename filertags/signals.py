import hashlib
import os.path
import re
import urlparse
from StringIO import StringIO

from django.core.cache import cache
from django.core.files.uploadedfile import UploadedFile
import django.core.files.base as base
from django.db.models import signals

from filer.models.filemodels import File
from filer.models.imagemodels import Image

from templatetags.filertags import filerfile, get_filerfile_cache_key


_LOGICAL_URL_TEMPLATE = "/* logicalurl('%s') */"
_RESOURCE_URL_TEMPLATE = "url('%s') " + _LOGICAL_URL_TEMPLATE
_RESOURCE_URL_REGEX = re.compile(r"\burl\(([^\)]*)\)")

_COMMENT_REGEX = re.compile(r"/\*.*?\*/")
_ALREADY_PARSED_MARKER = '/* Filer urls already resolved */'


def _is_in_clipboard(filer_file):
    return filer_file.folder is None


def _construct_logical_folder_path(filer_file):
    return os.path.join(*(folder.name for folder in filer_file.logical_path))


def _get_commented_regions(content):
    return [(m.start(), m.end()) for m in re.finditer(_COMMENT_REGEX, content)]


def _is_in_memory(file_):
    return isinstance(file_, UploadedFile)


class UnicodeContentFile(base.ContentFile):
    """
    patched due to cStringIO.StringIO constructor bug with unicode strings
    """
    def __init__(self, content, name=None):
        super(UnicodeContentFile, self).__init__(content, name=name)
        self.file = StringIO()
        self.file.writelines(content)
        self.file.reset()


def _rewrite_file_content(filer_file, new_content):
    if _is_in_memory(filer_file.file.file):
        filer_file.file.seek(0)
        filer_file.file.write(new_content)
    else:
        # file_name = filer_file.original_filename
        storage = filer_file.file.storage
        fp = UnicodeContentFile(new_content, filer_file.file.name)
        filer_file.file.file = fp
        filer_file.file.name = storage.save(filer_file.file.name, fp)
    sha = hashlib.sha1()
    sha.update(new_content)
    filer_file.sha1 = sha.hexdigest()
    filer_file._file_size = len(new_content)


def _is_css(filer_file):
    if filer_file.name:
        return filer_file.name.endswith('.css')
    else:
        return filer_file.original_filename.endswith('.css')


def resolve_resource_urls(instance, **kwargs):
    """Post save hook for css files uploaded to filer.
    It's purpose is to resolve the actual urls of resources referenced
    in css files.

    django-filer has two concepts of urls:
    * the logical url: media/images/foobar.png
    * the actual url: filer_public/2012/11/22/foobar.png

    The css as written by the an end user uses logical urls:
    .button.nice {
        background: url('../images/misc/foobar.png');
        -moz-box-shadow: inset 0 1px 0 rgba(255,255,255,.5);
    }

    In order for the resources to be found, the logical urls need to be
    replaced with the actual urls.

    Whenever a css is saved it parses the content and rewrites all logical
    urls to their actual urls; the logical url is still being saved
    as a comment that follows the actual url. This comment is needed for
    the behaviour described at point 2.

    After url rewriting the above css snippet will look like:
    .button.nice {
       background: url('filer_public/2012/11/22/foobar.png') /* logicalurl('media/images/misc/foobar.png') /* ;
       -moz-box-shadow: inset 0 1px 0 rgba(255,255,255,.5);
    }
    """
    if not _is_css(instance):
        return
    css_file = instance
    if _is_in_clipboard(css_file):
        return
    content = css_file.file.read()
    if content.startswith(_ALREADY_PARSED_MARKER):
        # this css' resource urls have already been resolved
        # this happens when moving the css in and out of the clipboard
        # multiple times
        return

    logical_folder_path = _construct_logical_folder_path(css_file)
    commented_regions = _get_commented_regions(content)
    local_cache = {}

    def change_urls(match):
        for start, end in commented_regions:
            # we don't make any changes to urls that are part of commented regions
            if start < match.start() < end or start < match.end() < end:
                return match.group()
        # strip spaces and quotes
        url = match.group(1).strip('\'\" ')
        parsed_url = urlparse.urlparse(url)
        if parsed_url.netloc:
            # if the url is absolute, leave it unchaged
            return match.group()
        relative_path = url
        logical_file_path = os.path.normpath(
            os.path.join(logical_folder_path, relative_path))
        if not logical_file_path in local_cache:
            local_cache[logical_file_path] = _RESOURCE_URL_TEMPLATE % (
                filerfile(logical_file_path), logical_file_path)
        return local_cache[logical_file_path]

    new_content = '%s\n%s' % (
        _ALREADY_PARSED_MARKER,
        re.sub(_RESOURCE_URL_REGEX, change_urls, content))
    _rewrite_file_content(css_file, new_content)


def update_referencing_css_files(instance, **kwargs):
    """Post save hook for any resource uploaded to filer that
    might be referenced by a css.
    The purpose of this hook is to update the actual url in all css files that
    reference the resource pointed by 'instance'.

    References are found by looking for comments such as:
    /* logicalurl('media/images/misc/foobar.png') */

    If the url between parentheses matches the logical url of the resource
    being saved, the actual url (which percedes the comment)
    is being updated.
    """
    if _is_css(instance):
        return
    resource_file = instance
    if _is_in_clipboard(resource_file):
        return
    if resource_file.name:
        resource_name = resource_file.name
    else:
        resource_name = resource_file.original_filename
    logical_file_path = os.path.join(
        _construct_logical_folder_path(resource_file),
        resource_name)
    css_files = File.objects.filter(original_filename__endswith=".css")
    for css in css_files:
        logical_url_snippet = _LOGICAL_URL_TEMPLATE % logical_file_path
        url_updating_regex = "%s %s" % (
            _RESOURCE_URL_REGEX.pattern, re.escape(logical_url_snippet))
        repl = "url('%s') %s" % (resource_file.url, logical_url_snippet)
        try:
            content = css.file.read()
            new_content = re.sub(url_updating_regex, repl, content)
        except IOError:
            # the filer database might have File entries that reference
            # files no longer phisically exist
            # TODO: find the root cause of missing filer files
            continue
        else:
            if content != new_content:
                _rewrite_file_content(css, new_content)
                css.save()


def clear_urls_cache(instance, **kwargs):
    """Clears urls cached by the filerfile tag. """
    logical_file_path = os.path.join(
        _construct_logical_folder_path(instance),
        instance.original_filename)
    cache_key = get_filerfile_cache_key(logical_file_path)
    cache.delete(cache_key)


signals.pre_save.connect(resolve_resource_urls, sender=File)
signals.post_save.connect(update_referencing_css_files, sender=File)
signals.post_save.connect(update_referencing_css_files, sender=Image)

signals.post_save.connect(clear_urls_cache, sender=File)
signals.post_save.connect(clear_urls_cache, sender=Image)
