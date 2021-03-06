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

import string

import factory

from django.contrib.auth import models as auth_models
from django.utils.crypto import get_random_string

from .. import models


class AnonymousUserFactory(factory.Factory):
    FACTORY_FOR = auth_models.AnonymousUser


class UserFactory(factory.DjangoModelFactory):
    FACTORY_FOR = auth_models.User

    username = factory.Sequence("user{0}".format)

    @classmethod
    def _prepare(cls, create, **kwargs):
        password = kwargs.pop('password', None)
        user = super(UserFactory, cls)._prepare(create, **kwargs)
        if password:
            user.set_password(password)
            if create:
                user.save()
        return user


class SuperUserFactory(UserFactory):
    username = factory.Sequence("admin{0}".format)
    is_superuser = True


class BlobMetadataFactory(factory.DjangoModelFactory):
    FACTORY_FOR = models.BlobMetadata

    oid = get_random_string(40, allowed_chars=string.hexdigits.lower())
    mimetype = "text/plain"


class TreeMetadataFactory(factory.DjangoModelFactory):
    FACTORY_FOR = models.TreeMetadata

    oid = get_random_string(40, allowed_chars=string.hexdigits.lower())
    mimetype = None

    @classmethod
    def _generate_next_sequence(cls):
        """managed = False"""
        return None


class TreePermissionFactory(factory.DjangoModelFactory):
    FACTORY_FOR = models.TreePermission

    parent_path = factory.Sequence("parent{0}".format)
    name = factory.Sequence("name{0}".format)
    user = factory.SubFactory(UserFactory)
