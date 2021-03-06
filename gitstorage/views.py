# -*- coding: utf-8 -*-
# Copyright 2013 Bors Ltd
# This file is part of django-gitstorage.
#
#    django-gitstorage is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Foobar is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Foobar.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import absolute_import, print_function, unicode_literals

from functools import update_wrapper
import logging
import operator
import unicodedata

from django.core.exceptions import PermissionDenied
from django.http.response import Http404, StreamingHttpResponse
from django.utils.decorators import classonlymethod
from django.views import generic as generic_views

import pygit2

from . import forms
from . import models
from . import storage as git_storage
from . import wrappers
from .utils import Path


logger = logging.getLogger(__name__)


class ObjectViewMixin(object):
    """API common to all Git object views.

    You want to inherit from BlobViewMixin, TreeViewMixin, etc.
    """
    allowed_types = ()
    # Attributes available when rendering the view
    storage = None
    path = None
    object = None
    metadata = None

    def check_object_type(self):
        """Some views only apply to blobs, other to trees."""
        logger.debug("check_object_type object=%s type=%s", self.object, self.object.type)
        if not self.object.type in self.allowed_types:
            raise Http404()

    def check_permissions(self):
        """Abstract, no implicit permission."""
        raise NotImplementedError()

    def filter_directories(self, tree, path):
        """
        Filter tree entries of the given tree by permission allowance.

        Should be in TreeViewMixin buy we want the root directories on every page.
        """
        user = self.request.user
        allowed_names = models.TreePermission.objects.allowed_names(user, path)

        # Don't use listdir to have direct access to the oid
        directories = []
        for entry in tree:
            # Hide hidden files
            if entry.name[0] == ".":
                continue
            if entry.filemode == wrappers.GIT_FILEMODE_TREE:
                name = entry.name.decode(git_storage.GIT_FILESYSTEM_ENCODING)
                if allowed_names is None or name in allowed_names:
                    directories.append({
                        'name': name,
                        'path': path.resolve(name),
                        'metadata': models.TreeMetadata(oid=entry.hex),
                    })
        return sorted(directories, key=operator.itemgetter('name'))

    def load_metadata(self):
        """Each object type has its own metadata model.

        Trees have an in-memory metadata built on the fly.
        """
        if self.object.type is pygit2.GIT_OBJ_BLOB:
            self.metadata = models.BlobMetadata.objects.get(pk=self.object.hex)
        elif self.object.type is pygit2.GIT_OBJ_TREE:
            self.metadata = models.TreeMetadata(oid=self.object.hex)

    def get_context_data(self, **kwargs):
        """Context variables for any type of Git object and on every page."""
        context = super(ObjectViewMixin, self).get_context_data(**kwargs)

        root_directories = self.filter_directories(self.storage.repository.tree, Path(""))

        breadcrumbs = []
        path = self.path
        while path:
            breadcrumbs.insert(0, path)
            path = Path(path.parent_path)

        context['path'] = self.path
        context['object'] = self.object
        context['metadata'] = self.metadata
        context['root_directories'] = root_directories
        context['breadcrumbs'] = breadcrumbs
        return context

    def dispatch(self, request, path, storage=None, git_obj=None, *args, **kwargs):
        """Filtering of hidden files and setting the instance attributes before dispatching."""
        logger.debug("dispatch self=%s path=%s object=%s args=%s kwargs=%s", self, path, git_obj, args, kwargs)
        path = Path(path)
        self.path = path

        name = path.name
        if name and name[0] == ".":
            raise PermissionDenied()

        if not storage:
            storage = git_storage.GitStorage()
        self.storage = storage

        if not git_obj:
            try:
                git_obj = self.storage.repository.find_object(path)
            except KeyError:
                raise Http404()

        self.object = git_obj
        self.check_object_type()
        self.load_metadata()

        logger.debug("calling check_permissions %s", self.check_permissions)
        self.check_permissions()

        return super(ObjectViewMixin, self).dispatch(request, path, *args, **kwargs)


class BlobViewMixin(ObjectViewMixin):
    """View that applies only to blobs (files).

    Permission is checked on the parent tree.
    """
    allowed_types = (pygit2.GIT_OBJ_BLOB,)

    def check_permissions(self):
        if not models.TreePermission.objects.is_allowed(self.request.user, Path(self.path.parent_path)):
            raise PermissionDenied()


class PreviewViewMixin(BlobViewMixin):

    def get(self, request, *args, **kwargs):
        content = self.storage.open(self.path)
        # "de\u0301po\u0302t.jpg" -> "dépôt.jpg"
        filename = unicodedata.normalize('NFKC', self.path.name)
        response = StreamingHttpResponse(content, content_type=self.metadata.mimetype)
        response['Content-Disposition'] = "inline; filename=%s" % (filename,)
        return response


class DownloadViewMixin(BlobViewMixin):

    def get(self, request, *args, **kwargs):
        content = self.storage.open(self.path)
        # "de\u0301po\u0302t.jpg" -> "dépôt.jpg"
        filename = unicodedata.normalize('NFKC', self.path.name)
        response = StreamingHttpResponse(content, content_type=self.metadata.mimetype)
        response['Content-Disposition'] = "attachment; filename=%s" % (filename,)
        return response


class DeleteViewMixin(BlobViewMixin):

    def post(self, request, *args, **kwargs):
        self.storage.delete(self.path)


class TreeViewMixin(ObjectViewMixin):
    """View that applies only to trees (directories).

    Permission is checked on the path itself.
    """
    allowed_types = (pygit2.GIT_OBJ_TREE,)

    def check_permissions(self):
        if not models.TreePermission.objects.is_allowed(self.request.user, self.path):
            raise PermissionDenied()

    def filter_files(self):
        # Always assume files are readable if the parent tree is
        oid_to_name = {}
        for entry in self.object:
            # Hide hidden files
            if entry.name[0] == ".":
                continue
            if entry.filemode in wrappers.GIT_FILEMODE_BLOB_KINDS:
                oid_to_name[entry.hex] = entry.name.decode(git_storage.GIT_FILESYSTEM_ENCODING)

        # Fetch metadata for all of the entries in a single query
        metadata = {}
        for value in models.BlobMetadata.objects.filter(pk__in=oid_to_name.iterkeys()):
            metadata[value.oid] = value

        files = []
        for oid, name in oid_to_name.iteritems():
            files.append({
                'name': name,
                'path': self.path.resolve(name),
                'metadata': metadata[oid],
            })
        return sorted(files, key=operator.itemgetter('name'))

    def get_context_data(self, **kwargs):
        context = super(TreeViewMixin, self).get_context_data(**kwargs)
        context['directories'] = self.filter_directories(self.object, self.path)
        context['files'] = self.filter_files()
        return context


class UploadViewMixin(TreeViewMixin):
    form_class = forms.UploadForm

    def form_valid(self, form):
        f = form.cleaned_data['file']
        path = self.path.resolve(f.name)
        self.storage.save(path, f)

        # Sync metadata
        blob = self.storage.repository.find_object(path)
        models.BlobMetadata.objects.create_from_name(f.name, blob.hex)

        return super(UploadViewMixin, self).form_valid(form)


class SharesViewMixin(TreeViewMixin):
    form_class = forms.RemoveUsersForm

    def get_form(self, form_class):
        current_permissions = models.TreePermission.objects.current_permissions(self.path)
        current_user_ids = current_permissions.values_list('user', flat=True)

        return form_class(current_user_ids, **self.get_form_kwargs())

    def form_valid(self, form):
        users = form.cleaned_data['users']
        models.TreePermission.objects.remove(users, self.path)

        return super(SharesViewMixin, self).form_valid(form)


class ShareViewMixin(TreeViewMixin):
    form_class = forms.AddUsersForm

    def get_form(self, form_class):
        current_permissions = models.TreePermission.objects.current_permissions(self.path)
        current_user_ids = current_permissions.values_list('user', flat=True)

        return form_class(current_user_ids, **self.get_form_kwargs())

    def form_valid(self, form):
        users = form.cleaned_data['users']
        models.TreePermission.objects.add(users, self.path)

        return super(ShareViewMixin, self).form_valid(form)


class AdminPermissionMixin(object):
    """Enforce permission to require superuser, whatever the Git object type."""

    def check_permissions(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied()
        super(AdminPermissionMixin, self).check_permissions()


class RepositoryView(ObjectViewMixin, generic_views.View):
    """Map URL path to the Git object that would be found in the working directory, then return the dedicated view.

    This is the only concrete class view, though useless without a configured "type_to_view".
    """

    type_to_view_class = {
        #pygit2.GIT_OBJ_TREE: MyTreeView,
        #pygit2.GIT_OBJ_BLOB: MyBlobView,
    }

    @classonlymethod
    def as_view(cls, **initkwargs):
        """
        Main entry point for a request-response process.

        Borrowed from django.views.generic.View.
        """
        # sanitize keyword arguments
        for key in initkwargs:
            if key in cls.http_method_names:
                raise TypeError("You tried to pass in the %s method name as a "
                                "keyword argument to %s(). Don't do that."
                                % (key, cls.__name__))
            if not hasattr(cls, key):
                raise TypeError("%s() received an invalid keyword %r. as_view "
                                "only accepts arguments that are already "
                                "attributes of the class." % (cls.__name__, key))

        def view(request, path, *args, **kwargs):
            # BEGIN gitstorage specific
            storage = kwargs['storage'] = git_storage.GitStorage()

            # Path methods must be mapped in the URLconf
            path = Path(path)
            if path.name and path.name[0] == ";":
                raise Http404()

            try:
                git_obj = kwargs['git_obj'] = storage.repository.find_object(path)
            except (KeyError, pygit2.GitError):
                raise Http404()

            # Find a view class dedicated to this object's type
            try:
                view_class = cls.type_to_view_class[git_obj.type]
            except KeyError:
                raise PermissionDenied()
            # END gitstorage specific

            self = view_class(**initkwargs)
            if hasattr(self, 'get') and not hasattr(self, 'head'):
                self.head = self.get
            self.request = request
            self.args = args
            self.kwargs = kwargs
            return self.dispatch(request, path, *args, **kwargs)

        # take name and docstring from class
        update_wrapper(view, cls, updated=())

        # and possible attributes set by decorators
        # like csrf_exempt from dispatch
        update_wrapper(view, cls.dispatch, assigned=())
        return view
