# Copyright 2002 Ben Escoto
#
# This file is part of duplicity.
#
# Duplicity is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 3 of the License, or (at your
# option) any later version.
#
# Duplicity is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with duplicity; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

import os
import time

import duplicity.backend
import duplicity.globals as globals
import duplicity.log as log

class BotoBackend(duplicity.backend.Backend):
    """
    Backend for Amazon's Simple Storage System, (aka Amazon S3), though
    the use of the boto module, (http://code.google.com/p/boto/).

    To make use of this backend you must export the environment variables
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY with your Amazon Web 
    Services key id and secret respectively.
    """

    def __init__(self, parsed_url):
        duplicity.backend.Backend.__init__(self, parsed_url)
        try:
            from boto.s3.connection import S3Connection
            from boto.s3.key import Key
            assert hasattr(S3Connection, 'lookup')

            # Newer versions of boto default to using
            # virtual hosting for buckets as a result of
            # upstream deprecation of the old-style access
            # method by Amazon S3. This change is not
            # backwards compatible (in particular with
            # respect to upper case characters in bucket
            # names); so we default to forcing use of the
            # old-style method unless the user has
            # explicitly asked us to use new-style bucket
            # access.
            #
            # Note that if the user wants to use new-style
            # buckets, we use the subdomain calling form
            # rather than given the option of both
            # subdomain and vhost. The reason being that
            # anything addressable as a vhost, is also
            # addressable as a subdomain. Seeing as the
            # latter is mostly a convenience method of
            # allowing browse:able content semi-invisibly
            # being hosted on S3, the former format makes
            # a lot more sense for us to use - being
            # explicit about what is happening (the fact
            # that we are talking to S3 servers).

            try:
                from boto.s3.connection import OrdinaryCallingFormat
                from boto.s3.connection import SubdomainCallingFormat
                cfs_supported = True
                calling_format = OrdinaryCallingFormat()
            except ImportError:
                cfs_supported = False
                calling_format = None

            if globals.s3_use_new_style:
                if cfs_supported:
                    calling_format = SubdomainCallingFormat()
                else:
                    log.FataError("Use of new-style (subdomain) S3 bucket addressing was"
                                  "requested, but does not seem to be supported by the "
                                  "boto library. Either you need to upgrade your boto "
                                  "library or duplicity has failed to correctly detect "
                                  "the appropriate support.")
            else:
                if cfs_supported:
                    calling_format = OrdinaryCallingFormat()
                else:
                    calling_format = None

        except ImportError:
            log.FatalError("This backend  (s3) requires boto library, version 0.9d or later, "
                           "(http://code.google.com/p/boto/).")

        if not os.environ.has_key('AWS_ACCESS_KEY_ID'):
            raise BackendException("The AWS_ACCESS_KEY_ID environment variable is not set.")

        if not os.environ.has_key('AWS_SECRET_ACCESS_KEY'):
            raise BackendException("The AWS_SECRET_ACCESS_KEY environment variable is not set.")

        if parsed_url.scheme == 's3+http':
            # Use the default Amazon S3 host.
            self.conn = S3Connection()
        else:
            assert parsed_url.scheme == 's3'
            self.conn = S3Connection(host=parsed_url.hostname)

        if hasattr(self.conn, 'calling_format'):
            if calling_format is None:
                log.FatalError("It seems we previously failed to detect support for calling "
                               "formats in the boto library, yet the support is there. This is "
                               "almost certainly a duplicity bug.")
            else:
                self.conn.calling_format = calling_format

        # This folds the null prefix and all null parts, which means that:
        #  //MyBucket/ and //MyBucket are equivalent.
        #  //MyBucket//My///My/Prefix/ and //MyBucket/My/Prefix are equivalent.
        self.url_parts = filter(lambda x: x != '', parsed_url.path.split('/'))

        if self.url_parts:
            self.bucket_name = self.url_parts.pop(0)
        else:
            # Duplicity hangs if boto gets a null bucket name.
            # HC: Caught a socket error, trying to recover
            raise BackendException('Boto requires a bucket name.')

        self.bucket = self.conn.lookup(self.bucket_name)
        self.key_class = Key

        if self.url_parts:
            self.key_prefix = '%s/' % '/'.join(self.url_parts)
        else:
            self.key_prefix = ''

        self.straight_url = duplicity.backend.strip_auth_from_url(parsed_url)

    def put(self, source_path, remote_filename=None):
        if not self.bucket:
            if globals.s3_european_buckets:
                if not globals.s3_use_new_style:
                    log.LogFatal("European bucket creation was requested, but not new-style "
                                 "bucket addressing (--s3-use-new-style)")
                from boto.s3.connection import Location
                self.bucket = self.conn.create_bucket(self.bucket_name, location = Location.EU)
            else:
                self.bucket = self.conn.create_bucket(self.bucket_name)
        if not remote_filename:
            remote_filename = source_path.get_filename()
        key = self.key_class(self.bucket)
        key.key = self.key_prefix + remote_filename
        for n in range(1, globals.num_retries+1):
            log.Log("Uploading %s/%s" % (self.straight_url, remote_filename), 5)
            try:
                key.set_contents_from_filename(source_path.name, {'Content-Type': 'application/octet-stream'})
                return
            except:
                pass
            log.Log("Upload '%s/%s' failed (attempt #%d)" % (self.straight_url, remote_filename, n), 1)
            time.sleep(30)
        log.Log("Giving up trying to upload %s/%s after %d attempts" % (self.straight_url, remote_filename, globals.num_retries), 1)
        raise BackendException("Error uploading %s/%s" % (self.straight_url, remote_filename))

    def get(self, remote_filename, local_path):
        key = self.key_class(self.bucket)
        key.key = self.key_prefix + remote_filename
        for n in range(1, globals.num_retries+1):
            log.Log("Downloading %s/%s" % (self.straight_url, remote_filename), 5)
            try:
                key.get_contents_to_filename(local_path.name)
                local_path.setdata()
                return
            except:
                pass
            log.Log("Download %s/%s failed (attempt #%d)" % (self.straight_url, remote_filename, n), 1)
            time.sleep(30)
        log.Log("Giving up trying to download %s/%s after %d attempts" % (self.straight_url, remote_filename, globals.num_retries), 1)
        raise BackendException("Error downloading %s/%s" % (self.straight_url, remote_filename))

    def list(self):
        filename_list = []
        if self.bucket:
            # We add a 'd' to the prefix to make sure it is not null (for boto) and
            # to optimize the listing of our filenames, which always begin with 'd'.
            # This will cause a failure in the regression tests as below:
            #   FAIL: Test basic backend operations
            #   <tracback snipped>
            #   AssertionError: Got list: []
            #   Wanted: ['testfile']
            # Because of the need for this optimization, it should be left as is.
            #for k in self.bucket.list(prefix = self.key_prefix + 'd', delimiter = '/'):
            for k in self.bucket.list(prefix = self.key_prefix, delimiter = '/'):
                try:
                    filename = k.key.replace(self.key_prefix, '', 1)
                    filename_list.append(filename)
                    log.Log("Listed %s/%s" % (self.straight_url, filename), 9)
                except AttributeError:
                    pass
        return filename_list

    def delete(self, filename_list):
        for filename in filename_list:
            self.bucket.delete_key(self.key_prefix + filename)
            log.Log("Deleted %s/%s" % (self.straight_url, filename), 9)

duplicity.backend.register_backend("s3", BotoBackend)
duplicity.backend.register_backend("s3+http", BotoBackend)
