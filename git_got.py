#!/usr/bin/env python2

# Copyright (c) 2015, 2016  Jake Cheuvront, Chris Lalancette, Manuel Naranjo
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.

# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.

# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import sys
import os
import hashlib
import json
import fnmatch
import requests
import dulwich.porcelain
import errno
import requests_toolbelt.multipart.encoder
import re
import paramiko
import urlparse
import getpass
import getopt
import logging
import ftplib
import collections
import contextlib
import urllib
import shutil

VERSION = 1

remote_objs = []

local_cache_path = os.path.expanduser('~/.git-got-cache')

def load_with_cache(fn):
    def wrapped(self, filename, checksum, force, *args, **kwargs):
        if not self:
            return fn(self, *args, **kwargs)
        if self.load_from_cache(filename, checksum, force):
            return
        fn(self, filename, checksum, *args, **kwargs)
        self.store_in_cache(filename, checksum)
    return wrapped

def store_with_cache(fn):
    def wrapped(self, filename, checksum, *args, **kwargs):
        if not self:
            return fn(self, *args, **kwargs)
        fn(self, filename, checksum, *args, **kwargs)
        self.store_in_cache(filename, checksum)
    return wrapped

class Remote(object):
    def __init__(self, configuration):
        self.configuration = configuration

    def version(self):
        return self.configuration['version']

    def remote_name(self):
        return str(self.configuration['name'])

    def remote_default(self):
        return self.configuration['default']

    def remote_type(self):
        return self.configuration['remote_type']

    def remote_url(self):
        return self.configuration['remote']

    def load(self, filename, checksum):
        raise Exception("Load not implemented for this remote!")

    def generate_path_for_cache(self, checksum):
        checksum = checksum.encode('utf-8').lower()
        return os.path.join(local_cache_path, checksum[0], checksum[1:])

    def load_from_cache(self, filename, checksum, force):
        '''
        Try loading the given file from cache instead of getting from the remote
        server

        filename: local path
        checksum: original checksum
        '''
        if force:
            return False

        path = self.generate_path_for_cache(checksum)
        logging.debug('checking for file in cache: %s', path)
        if not os.path.isfile(path):
            logging.debug('file not available in cache')
            return False

        logging.debug('retrieving file from cache')
        size = os.path.getsize(path)

        try:
            copy_file(path, filename, "Downloading (cached)", filename)
            return True
        except Exception, e:
            logging.exception('Failed retrieving from cache', e)
        return False

    def store(self, filename, checksum):
        raise Exception("Store not implemented for this remote!")

    def store_in_cache(self, filename, checksum):
        '''
        Store the file in our local cache

        filename: source filename
        checksum: checksum from file
        '''
        path = self.generate_path_for_cache(checksum)
        logging.debug('checking for file in cache: %s', path)
        if os.path.isfile(path):
            logging.debug('file already in our cache ignore cache updating')
            return False

        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except Exception, e:
                logging.exception('Failed creating parent for cache', e)
                return False

        logging.debug('storing into our cache: %s' % checksum)
        try:
            shutil.copyfile(filename, path)
            return True
        except Exception, e:
            logging.exception('Failed storing object into our cache', e)
        return False

    def scheme(self):
        raise Exception("Scheme not implemented for this remote!")

############################ SCP BACKEND #####################################
class SCP(Remote):
    def __init__(self, configuration):
        Remote.__init__(self, configuration)
        self.direction = "Downloading"
        self.filename = "Unset"

    def _print_total(self, transferred, total):
        print_transfer_string(transferred, total, self.filename, self.direction)

    def _get_location_info_scp(self):
        parser = urlparse.urlparse(self.configuration['remote'])
        username = parser.username
        if username is None:
            username = getpass.getuser()
        return (parser.hostname, parser.path, username)

    def _ssh_sftp_connect(self):
        (hostname, remote_dir, username) = self._get_location_info_scp()
        logging.debug("hostname: %s, remote_dir: %s, username: %s" % (hostname,
                                                                      remote_dir,
                                                                      username))
        ssh = paramiko.SSHClient()
        ssh.load_host_keys(os.path.expanduser(os.path.join("~", ".ssh", "known_hosts")))
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname, username=username)
        sftp = ssh.open_sftp()
        sftp.chdir(remote_dir)
        return (ssh, sftp)

    @load_with_cache
    def load(self, filename, checksum):
        logging.debug("load_scp")
        (ssh, sftp) = self._ssh_sftp_connect()

        remotefile = '%s.got' % (checksum)
        self.direction = "Downloading"
        self.filename = filename
        sftp.get(remotefile, filename, callback=self._print_total)
        sys.stdout.write("\n")
        sftp.close()
        ssh.close()

    @store_with_cache
    def store(self, filename, checksum):
        logging.debug("store_scp")
        (ssh, sftp) = self._ssh_sftp_connect()

        remotefile = '%s.got' % (checksum)

        # here we do an optimization; if the remote file with the right filename
        # already exists, we don't need to upload it again.  Just get out
        try:
            sftp.stat(remotefile)
            logging.debug("File existed on remote, skipping upload...")
            sftp.close()
            ssh.close()
            return
        except IOError:
            logging.debug("Uploading file to remote...")

        self.direction = "Uploading"
        self.filename = filename
        sftp.put(filename, remotefile, callback=self._print_total)
        sys.stdout.write("\n")
        sftp.close()
        ssh.close()

    def scheme(self):
        return ['ssh']

############################ SRR BACKEND #####################################
class SRR(Remote):
    def __init__(self, configuration):
        Remote.__init__(self, configuration)
        self.last_mb = -1
        self.filename = "Unset"
        self.upload_len = -1

    def _get_location_info_srr(self):
        logging.debug("srr._get_location_info_srr")
        parser = urlparse.urlparse(self.configuration['remote'])
        return (parser.scheme, parser.hostname, os.path.basename(parser.path))

    def upload_cb(self, monitor):
        print_transfer_string(monitor.bytes_read, self.upload_len, self.filename,
                              "Uploading")

    @store_with_cache
    def store(self, filename, checksum):
        logging.debug("store_srr")
        (scheme, server_name, parent_id) = self._get_location_info_srr()
        target_id = ''
        remote_path = '%s' % checksum
        description = 'Got storage for %s @ TBD hashtag' % (filename)

        # first check to see if the file already exists in the SRR
        r = requests.get('%s://%s/srr/api/file_metadata/sha256/%s' % (scheme, server_name, checksum))
        if r.status_code == 200:
            logging.debug("File existed on remote, skipping upload...")
            return

        # if the response was 404, the object was not in the store and thus we
        # need to upload it
        logging.debug("Uploading file to remote...")

        e = requests_toolbelt.multipart.encoder.MultipartEncoder(fields={
          'parent_id' : parent_id,
          'target_id' : '',
          'description' : description,
          'file' : (checksum, open(filename, 'rb'), 'application/octet-stream')})

        # In requests_toolbelt 0.3 and earlier, the MultipartEncoder class had
        # a __len__ method, so you could call len(e) to get the total length.
        # In requests_toolbelt 0.4 and later, the MultipartEncoder class has a
        # method called len.  To accommodate both, we first try the old
        # way (len(e)), and if that fails with a TypeError we try the new way.
        try:
            self.upload_len = len(e)
        except TypeError:
            self.upload_len = e.len

        self.filename = filename
        m = requests_toolbelt.multipart.encoder.MultipartEncoderMonitor(e,
                                                                        self.upload_cb)

        response = requests.post('%s://%s/srr/api/add_file' % (scheme, server_name), data = m, headers = {'Content-Type' : m.content_type})

        if response.status_code != 200:
            raise Exception("%s: %s" % (response.reason, response.status_code))
        sys.stdout.write("\n")
        new_id_re = re.compile(r' file_id=(\d+)\s*$')
        m = new_id_re.search(response.text)
        if m:
            return int(m.group(1))
        else:
            raise Exception("Unexpected result from SRR")

    def _get_remote_path_srr(self, scheme, server, checksum):
        response = requests.get('%s://%s/srr/api/file_metadata/sha256/%s' % (scheme, server, checksum))
        if response.status_code == 404:
            raise Exception("Unexpected result from SRR: %d" % response.status_code)
        return urllib.quote(response.json()['url'].encode('utf-8'), ':/%')

    def _curlprogress(self, down_total, down_current, up_total, up_current):
        if down_total == 0:
            return

        current_mb = int(down_current) / 1048576
        if current_mb > self.last_mb or down_current == down_total:
            self.last_mb = current_mb
            print_transfer_string(down_current, down_total, self.filename,
                                  "Downloading")

    @load_with_cache
    def load(self, filename, checksum):
        checksum = checksum.encode('utf-8')
        logging.debug("load_srr %s %s" % (filename, checksum))
        (scheme, server, parent_id) = self._get_location_info_srr()
        server = server.encode('utf-8')
        parent_id = parent_id.encode('utf-8')

        path = self._get_remote_path_srr(scheme, server, checksum)
        logging.debug("load_srr, filename %s, path %s" % (filename, path))

        self.last_mb = -1
        self.filename = filename
        total_length = 0
        count = 0
        r = requests.get(path, stream=True)
        with open(filename, 'wb') as f:
            total_length = int(r.headers['Content-Length'])
            for chunk in r.iter_content(chunk_size=4096):
                if chunk: # filter out keep-alive new chunks
                    count = count+len(chunk)
                    f.write(chunk)
                    print_transfer_string(count, total_length, filename, "Downloading")

        sys.stdout.write("\n")

    def scheme(self):
        return ['http','https']

############################ FILE BACKEND #####################################
class File(Remote):
    def __init__(self, configuration):
        Remote.__init__(self, configuration)
        self.block_size = 1048576

    @store_with_cache
    def store(self, filename, checksum):
        logging.debug("store_file")
        parser = urlparse.urlparse(self.configuration['remote'])
        dstpath = os.path.join(parser.path, checksum + ".got")
        if os.path.exists(dstpath):
            logging.debug("File existed on remote, skipping upload...")
            return

        copy_file(filename, dstpath, "Uploading", filename, self.block_size)

    @load_with_cache
    def load(self, filename, checksum):
        logging.debug("load_file")
        parser = urlparse.urlparse(self.configuration['remote'])

        copy_file(os.path.join(parser.path, checksum + ".got"), filename,
                  "Downloading", filename, self.block_size)

    def scheme(self):
        return ['file']

############################ FTP BACKEND #####################################
class FTP(Remote):
    def __init__(self, configuration):
        Remote.__init__(self, configuration)
        self.block_size = 8192
        self.total_transferred = 0
        self.total = 0
        self.filename = None
        self.remote_file = None
        self.exists_on_remote = False
        self.download_fp = None

    def _transfer_cb(self, block):
        self.total_transferred += len(block)
        print_transfer_string(self.total_transferred, self.total, self.filename, "Uploading")

    def _file_exists_cb(self, name):
        if name == self.remote_file:
            self.exists_on_remote = True

    @store_with_cache
    def store(self, filename, checksum):
        logging.debug("store_ftp")
        parser = urlparse.urlparse(self.configuration['remote'])

        remotefile = '%s.got' % (checksum)

        ftp = ftplib.FTP(parser.hostname)
        ftp.login(parser.username, parser.password)
        ftp.set_pasv(True)
        # Change to the right directory.  Note that we strip off the starting
        # slash since that isn't generally what is wanted.
        ftp.cwd(parser.path[1:])

        self.remote_file = remotefile
        self.exists_on_remote = False
        ftp.retrlines("NLST", self._file_exists_cb)
        if self.exists_on_remote:
            logging.debug("File existed on remote, skipping upload...")
            return

        with open(filename, 'rb') as fp:
            self.total = file_length(fp)
            self.total_transferred = 0
            self.filename = filename
            ftp.storbinary('STOR %s' % remotefile, fp, self.block_size, self._transfer_cb)

        print_transfer_string(self.total, self.total, self.filename, "Uploading")
        ftp.close()

        sys.stdout.write("\n")

    def _write_and_print_cb(self, block):
        self.total_transferred += len(block)
        self.download_fp.write(block)
        print_transfer_string(self.total_transferred, self.total, self.filename, "Downloading")

    @load_with_cache
    def load(self, filename, checksum):
        logging.debug("load_ftp")
        parser = urlparse.urlparse(self.configuration['remote'])

        ftp = ftplib.FTP(parser.hostname)
        ftp.login(parser.username, parser.password)
        ftp.set_pasv(True)
        # Change to the right directory.  Note that we strip off the starting
        # slash since that isn't generally what is wanted.
        ftp.cwd(parser.path[1:])

        remotefile = '%s.got' % (checksum)

        self.total = ftp.size(remotefile)
        self.filename = filename
        self.total_transferred = 0
        with open(filename, 'wb') as self.download_fp:
            ftp.retrbinary("RETR %s" % remotefile, self._write_and_print_cb)

        ftp.close()

        sys.stdout.write("\n")

    def scheme(self):
        return ['ftp']

def RemoteObjFactory(remote_type, remote, configuration):
    if remote_type == 'srr':
        obj = SRR(configuration)
    elif remote_type == 'scp':
        obj = SCP(configuration)
    elif remote_type == 'file':
        obj = File(configuration)
    elif remote_type == 'ftp':
        obj = FTP(configuration)
    else:
        raise GotException("Invalid remote type '%s'" % remote_type, need_usage=True)

    parser = urlparse.urlparse(remote)
    objscheme = obj.scheme()
    if parser.scheme not in obj.scheme():
        raise GotException("Remote of type '%s' must have URL scheme of '%s'" % (remote_type, objscheme))

    return obj

##################### CUSTOM EXCEPTION CLASS #################################
class GotException(Exception):
    def __init__(self, msg, need_usage=False):
        self.msg = msg
        self.need_usage = need_usage
    def __str__(self):
        usagestr = ""
        if self.need_usage:
            if self.msg != "":
                usagestr += "\n\n"
            usagestr += usage()

        return self.msg + usagestr

############################# HELPERS #######################################
def copy_file(srcpath, dstpath, prefix, outfilename, blocksize=1048576):
    '''
    A function to copy a file from srcpath to dstpath.  While copying it will
    print a progress string with the prefix and outfilename specified.

    @param srcpath      The source to copy data from
    @param dstpath      The destination to write data to
    @param prefix       The prefix to print in the progress string
    @param outfilename  The filename to print in the progress string
    @param blocksize    How much data to transfer at a time; defaults to 1048576
    '''
    # FIXME: we may want to add a check to ensure that dirname(srcpath) exists
    # before starting the transfer.  Otherwise the error message that happens
    # isn't entirely clear which part is missing.

    with open(srcpath, 'rb') as src:
        with open(dstpath, 'wb') as dst:
            total_len = file_length(src)
            transferred = 0

            while True:
                block = src.read(blocksize)

                if not block:
                    # end of file
                    break
                else:
                    dst.write(block)

                print_transfer_string(transferred, total_len, outfilename, prefix)
                transferred += len(block)

            print_transfer_string(total_len, total_len, outfilename, prefix)
            sys.stdout.write("\n")

def print_transfer_string(transferred, total, filename, prefix):
    """
    A function to print out what percentage of a transfer has happened to which
    file, and in which direction (up or down).

    @param transferred  The number of bytes transferred so far
    @param total        The total number of bytes in transfer
    @param filename     The local filename being downloaded or uploaded
    @param prefix       A string that will be put on the front of the output
    """
    if total > 1073741824:
        suffix = "GB"
        divider = 1073741824
    elif total > 1048576:
        suffix = "MB"
        divider = 1048576
    elif total > 1024:
        suffix = "KB"
        divider = 1024
    else:
        suffix = "bytes"
        divider = 1

    divided_tran = int(transferred / divider)
    divided_total = int(total / divider)
    if total == 0:
        # if the total was zero (zero-byte file), then avoid a divide by zero
        # and just assume the transfer is complete
        percent = 100
    else:
        percent = int(transferred * 100 / total)
    # The "fixed" length for our printed string is the length of the prefix,
    # plus one for the space, plus one for the opening quote for the filename,
    # plus one for the closing quote for the filename, plus one for a space,
    # plus the length of the total size (used to ensure the length stays
    # constant), plus one for the divide sign, plus one for the length of the
    # total number, plus one for a space, plus the length of the suffix (GB,
    # MB, etc), plus one for a space, plus one for the opening parentheses for
    # the percentage, plus three for the percentage (fixed so that we don't
    # change how we truncate the filename at the end of the transfer), plus one
    # for the percent sign, plus one for the closing parentheses.
    strlen_no_file = len(prefix) + 1 + 1 + 1 + 1 + len(str(divided_total)) + 1 + len(str(divided_total)) + 1 + len(suffix) + 1 + 1 + 3 + 1 + 1

    # Now that we know the fixed length of the string, we can figure out how
    # much of the filename we can afford to print.  Note that we want to print
    # the end of the filename, as that is likely to be the most helpful.  We do
    # this by reversing the string (filename[::-1]), taking the
    # first 80-strlen_no_file characters, then reversing back.
    if strlen_no_file + len(filename) > 80:
        filename = filename[::-1][:(80-strlen_no_file)][::-1]
    sys.stdout.write("\r")
    sys.stdout.write("{0} '{1}' {2}/{3} {4} ({5}%)".format(prefix,
                                                           filename,
                                                           divided_tran,
                                                           divided_total,
                                                           suffix, percent))

    sys.stdout.flush()

def usage():
    return """git got <command> [<args>]

  The available git got commands are:
    init <name> <type> <url>         Initialize the default remote, where <type>
                                     is one of 'scp', 'srr', 'file', or 'ftp'.
                                     The <name> is a unique name used to refer to
                                     the repository.  The <url> is the fully
                                     qualified URL to the remote.

    get [-f]      [<file>...]        With no arguments, retrieve all remote files
                                     to the local working area.  With one or more
                                     arguments, retrieve just those remote files
                                     to the local working area.  By default, if
                                     the file already exists locally, the "get"
                                     command will skip downloading the file.  The
                                     optional -f flag forces git got to download
                                     the file from the remote, even if it already
                                     exists locally or is in the local per-user
                                     cache (located at ~/.git-got-cache).

    add [-r <remote] [-R] <file>...  Add one or more files to the remote
                                     repository.  By default, directories are not
                                     allowed.  The optional -r argument allows the
                                     user to specify which remote to use; if not
                                     specified, the default remote is used.  The
                                     optional -R flag can be used to recurse into
                                     the specified directory, adding all the
                                     files not already managed by git into git
                                     got.

    status [-v] [<file>...]          With no arguments, request the status of all
                                     got tracked files.  With one or more
                                     arguments, request the status of the named
                                     files.  The optional -v argument requests
                                     verbose mode where the status of all files
                                     are shown, even the ones that haven't
                                     changed.

    reset <file>...                  Overwrite one or more local got files with
                                     the remote copy.  Note that directories are
                                     not allowed.

    rm [-R] <file>...                Remove the specified file or files from got
                                     tracking.  Note that this does not remove the
                                     file from the remote. By default, directories
                                     are not allowed.  The optional -R argument
                                     can be used to recurse into the specified
                                     directory, removing all files managed by
                                     git got.

    rm_local                         Remove all of the locally download files.
                                     This will cause all of them to be fetched on
                                     the next "git got get".

    mv <old> <new>                   Move the local got tracking file from <old>
                                     to <new>.  Note that this does not change the
                                     file on the remote.  Also note that
                                     directories are not allowed.

    add_remote <name> <type> <url>   Add an additional remote for storage.  The
                                     arguments are the same as for init.

    remove_remote <name>             Remove a remote from the repository.  This
                                     will only succeed if the remote is not the
                                     default and if there are no files in the
                                     repository linked to that remote.

    list_remotes                     List all of the remotes that are configured
                                     for got.  The remote printed with a star is
                                     the default.

    chmod <file> <mode>              Change the permission bits for the specified
                                     file.  Note that these bits are automatically
                                     restored when the file is re-downloaded from
                                     the remote.

    clear-local-cache                Clear the git got cache available at
                                     ~/.git-got-cache.

    fill-local-cache                 Fills the git got local cache with the
                                     tracked files from the current project.

  """

def file_hash(filename):
    """
    Hash the contents of the specified file using SHA-256 and return the hash
    as a string.

    @param filename  The filename to hash the contents of
    @return String representing the SHA-256 hash of the file contents
    """
    hasher = hashlib.sha256()
    with open(filename, 'rb') as infp:
        while True:
            data = infp.read(8192)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()

def file_length(fp):
    old = fp.tell()
    fp.seek(0, 2)
    ret = fp.tell()
    fp.seek(old)
    return ret

def mkdir_p(path):
    """
    Function to make a directory and all intermediate directories as
    necessary.  The functionality differs from os.makedirs slightly, in
    that this function does *not* raise an error if the directory already
    exists.

    @param path  The directory path to create
    """
    if path is None:
        raise Exception("Path cannot be None")

    if path == '':
        # this can happen if the user did something like call os.path.dirname()
        # on a file without directories.  Since os.makedirs throws an exception
        # in that case, check for it here and allow it.
        return

    try:
        os.makedirs(path)
    except OSError as err:
        if err.errno != errno.EEXIST or not os.path.isdir(path):
            raise

def status_local(real_filename, got_checksum):
    """
    Determines if there are local changes made to the file specified in the
    filename parameter.  Assumes that the got_filename exists.

    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @return True if the file exists locally and is unchanged, False otherwise
    """
    if not os.path.exists(real_filename):
        logging.debug('status_local: Did not find file %s' % real_filename)
        return False
    sum1 = file_hash(real_filename)
    if sum1 != got_checksum:
        logging.debug('status_local: Got hash %s != file hash %s' % (sum1, got_checksum))
        return False
    return True

####################### WALKER AND CALLBACKS ##################################
def get_cb(repo, got_filename, real_filename, cb_params):
    """
    Fetches the specified file from the remote if necessary.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      Whether to force the transfer or not
    """
    force = cb_params
    try:
        logging.debug('get_cb: Using %s for local file' % real_filename)
        if not os.path.exists(got_filename):
            # this isn't a file tracked by got; this can happen if the user
            # asked to get or reset a file that got is not tracking
            raise Exception("'%s' is not tracked by got" % (real_filename))

        with open(got_filename, 'rb') as storagefp:
            gotconf = json.load(storagefp)

        if not force and status_local(real_filename, gotconf['sha-256']):
            logging.debug("File already exists, and has right checksum; skipping download...")
            return
        logging.debug("Downloading remote file...")
        for remote_obj in remote_objs:
            if remote_obj.remote_name() == gotconf['remote']:
                remote_obj.load(real_filename, gotconf['sha-256'], force)
                os.chmod(real_filename, gotconf['mode'])
                return
        raise Exception("Could not find remote '%s' for file '%s'" % (gotconf['remote'], real_filename))
    except Exception as e:
        raise GotException("Failed to retrieve file '%s': %s" % (real_filename, str(e)))

def reset_cb(repo, got_filename, real_filename, cb_params):
    """
    Resets the specified file to the version from the got database, re-downloading
    it from the remote if necessary.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      Ignored
    """
    get_cb(repo, got_filename, real_filename, cb_params)

def add_cb(repo, got_filename, real_filename, cb_params):
    """
    Adds a new file to the got database and uploads it to the remote.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      A string representing the remote to use
    """
    try:
        remote = cb_params
        remote_obj = None
        for tmp in remote_objs:
            if remote is None:
                if tmp.remote_default():
                    remote_obj = tmp
                    break
            else:
                if tmp.remote_name() == remote:
                    remote_obj = tmp
                    break

        if remote_obj is None:
            raise Exception("Remote named '%s' does not exist" % (remote))

        logging.debug('add_cb: Adding %s' % real_filename)
        csum = file_hash(real_filename)
        remote_obj.store(real_filename, csum)
        gotconf = { 'sha-256': csum, 'remote': remote_obj.remote_name(), 'mode': os.stat(real_filename).st_mode }
        with open(got_filename, 'wb') as out:
            json.dump(gotconf, out)
        dulwich.porcelain.add(repo, got_filename)

        # The user may be adding a new file, or updating a filename that already
        # exists.  If it is the former, we want to add the filenames to gitignore;
        # for the latter, we don't want to add duplicate entries.  Check that here
        # and do the right thing in both cases.
        already_in_gitignore = False
        if os.path.exists('.gitignore'):
            with open('.gitignore', 'rb') as gitigf:
                for line in gitigf.xreadlines():
                    if line == real_filename + "\n":
                        already_in_gitignore = True
                        break

        if not already_in_gitignore:
            with open('.gitignore', 'ab') as gitigf:
                gitigf.write('%s\n' % real_filename)
            dulwich.porcelain.add(repo, '.gitignore')
    except Exception as e:
        raise GotException("Failed to add '%s': %s" % (real_filename, str(e)))

def status_cb(repo, got_filename, real_filename, cb_params):
    """
    Retrieves the status of the filename specified.  Invoked from the main
    walker loop.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      Verbose or not
    """
    try:
        with open(got_filename, 'rb') as storagefp:
            gotconf = json.load(storagefp)

        if not os.path.exists(real_filename):
            return "Missing locally: '%s' (remote '%s')" % (real_filename, gotconf['remote'])

        if not status_local(real_filename, gotconf['sha-256']):
            return "Modified: '%s' (remote '%s')" % (real_filename, gotconf['remote'])
        # If we make it here, then the file exists locally and is the same as on
        # the remote.  If we are verbose, add this file to the list.
        verbose = cb_params
        if verbose:
            return "Unmodified: '%s' (remote '%s')" % (real_filename, gotconf['remote'])
    except Exception as e:
        raise GotException("Failed to get status of '%s': %s" % (real_filename, str(e)))

# get_tree_changes is essentially a copy of dulwich.porcelain.status(), but
# since dulwich 0.9.4 (in Ubuntu 14.04) doesn't have this implemented, we copy
# it here.
def get_tree_changes(repo):
    """Return add/delete/modify changes to tree by comparing index to HEAD.

    :param repo: repo path or object
    :return: dict with lists for each type of change
    """

    GitStatus = collections.namedtuple('GitStatus', 'staged')

    @contextlib.contextmanager
    def _noop_context_manager(obj):
        """Context manager that has the same api as closing but does nothing."""
        yield obj

    def open_repo_closing(path_or_repo):
        """Open an argument that can be a repository or a path for a repository.
        returns a context manager that will close the repo on exit if the argument
        is a path, else does nothing if the argument is a repo.
        """
        return _noop_context_manager(path_or_repo)

    with open_repo_closing(repo) as r:
        index = r.open_index()

        # Compares the Index to the HEAD & determines changes
        # Iterate through the changes and report add/delete/modify
        # TODO: call out to dulwich.diff_tree somehow.
        tracked_changes = {
          'add': [],
          'delete': [],
          'modify': [],
        }
        for change in index.changes_from_tree(r.object_store, r[b'HEAD'].tree):
            if not change[0][0]:
                tracked_changes['add'].append(change[0][1])
            elif not change[0][1]:
                tracked_changes['delete'].append(change[0][0])
            elif change[0][0] == change[0][1]:
                tracked_changes['modify'].append(change[0][0])
            else:
                raise AssertionError('git mv ops not yet supported')
        return GitStatus(tracked_changes)

def rm_cb(repo, got_filename, real_filename, cb_params):
    """
    Removes a file from got tracking.  Note that it does not remove the remote
    file, just the local tracking.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      Ignored
    """
    try:
        if not os.path.exists(got_filename):
            raise Exception("'%s' is not tracked by got" % (real_filename))

        # attempt to remove the real file, but if it is already gone, just go on
        logging.debug("Removing real file")
        try:
            os.remove(real_filename)
        except OSError:
            pass
        # now remove the got tracking file
        if got_filename in get_tree_changes(repo).staged['add']:
            # In theory we should use dulwich.porcelain.rm() here, but for some
            # reason it doesn't seem to work.  This is basically the same thing.
            index = repo.open_index()
            del index[got_filename]
            index.write()
            os.remove(got_filename)
        else:
            os.remove(got_filename)
            dulwich.porcelain.add(repo, got_filename)
        # now remove the entry from .gitignore
        logging.debug("Removing file from gitignore")
        with open('.gitignore', 'rb') as infp:
            with open('.tmp.gitignore', 'wb') as outfp:
                for line in infp.xreadlines():
                    if line != real_filename + "\n":
                        outfp.write(line)

        os.rename(".tmp.gitignore", ".gitignore")
        dulwich.porcelain.add(repo, ".gitignore")
    except Exception as e:
        raise GotException("Failed to remove '%s': %s" % (real_filename, str(e)))

def remove_remote_check_cb(repo, got_filename, real_filename, cb_params):
    """
    Check to see that whether this file is linked to the remote passed in as
    "cb_params[0]".  If it is, add the name of this file to the list of links
    stored in "cb_params[1]"

    @param repo           Dulwich repository object (ignored)
    @param got_filename   Got meta filename (ignored)
    @param real_filename  Real filename
    @param cb_params      The name of the remote
    """
    try:
        with open(got_filename, 'rb') as storagefp:
            gotconf = json.load(storagefp)

        remote_name = cb_params

        if gotconf['remote'] == remote_name:
            return real_filename
        else:
            return ''
    except Exception as e:
        raise GotException("Failed to check remote link '%s': %s" % (real_filename, str(e)))

def chmod_cb(repo, got_filename, real_filename, cb_params):
    """
    Change the permission bits on a got file.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      The new mode for the file
    """
    try:
        with open(got_filename, 'rb') as storagefp:
            gotconf = json.load(storagefp)

        newmode = int(cb_params, 0)

        if newmode == gotconf['mode']:
            return

        # first change the mode in the configuration
        gotconf['mode'] = newmode
        with open(got_filename, 'wb') as out:
            json.dump(gotconf, out)

        dulwich.porcelain.add(repo, got_filename)

        # if the file exists, change the mode
        if os.path.exists(real_filename):
            os.chmod(real_filename, newmode)
    except Exception as e:
        raise GotException("Failed to change mode on '%s': %s" % (real_filename, str(e)))

def mv_cb(repo, got_filename, real_filename, cb_params):
    """
    Moves a file in got tracking from one name to another.  Note that it does not
    affect the remote file, just the local tracking.

    @param repo           Dulwich repository object
    @param got_filename   Old got meta filename
    @param real_filename  Old real filename
    @param cb_params      New real filename
    """
    try:
        new_real_filename = cb_params

        if not os.path.exists(got_filename):
            raise Exception("'%s' is not tracked by got" % (real_filename))

        (base, filename) = os.path.split(new_real_filename)
        new_got_filename = os.path.join(base, '.%s.got' % filename)

        # attempt to move the real file; it may not exist, so ignore errors
        logging.debug("Moving real file from %s to %s" % (real_filename, new_real_filename))
        try:
            os.rename(real_filename, new_real_filename)
        except OSError:
            pass

        # now move the got tracking file
        logging.debug("Moving got tracking file from %s to %s" % (got_filename, new_got_filename))
        if got_filename in get_tree_changes(repo).staged['add']:
            # In theory we should use dulwich.porcelain.rm() here, but for some
            # reason it doesn't seem to work.  This is basically the same thing.
            index = repo.open_index()
            del index[got_filename]
            index.write()
            os.rename(got_filename, new_got_filename)
            dulwich.porcelain.add(repo, new_got_filename)
        else:
            os.rename(got_filename, new_got_filename)
            dulwich.porcelain.add(repo, got_filename)
            dulwich.porcelain.add(repo, new_got_filename)

        # now move the .gitignore entry
        logging.debug("Moving file in .gitignore")
        with open('.gitignore', 'rb') as infp:
            with open('.tmp.gitignore', 'wb') as outfp:
                for line in infp.xreadlines():
                    if line != real_filename + "\n":
                        outfp.write(line)
                outfp.write('%s\n' % new_real_filename)

            os.rename(".tmp.gitignore", ".gitignore")
        dulwich.porcelain.add(repo, ".gitignore")
    except Exception as e:
        raise GotException("Failed to move '%s' to '%s': %s" % (real_filename, new_real_filename, str(e)))

def rm_local_cb(repo, got_filename, real_filename, cb_params):
    """
    Removes the locally downloaded version of the real file.  This does not affect
    the got tracking or the remote file.
    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      None
    """
    try:
        os.remove(real_filename)
    except OSError:
        pass

def fill_local_cache_cb(repo, got_filename, real_filename, cb_params):
    """
    adds the file to the local cache if it exists and it's not present on the
    cache.

    @param repo           Dulwich repository object
    @param got_filename   Got meta filename
    @param real_filename  Real filename
    @param cb_params      Verbose or not
    """

    def get_remote(conf):
        for remote_obj in remote_objs:
            if remote_obj.remote_name() == conf['remote']:
                return remote_obj
        raise GotException("Remote '%s' not known" % conf['remote'])

    try:
        with open(got_filename, 'rb') as storagefp:
            gotconf = json.load(storagefp)

        if not os.path.exists(real_filename):
            return "Missing locally: '%s' (remote '%s')" % (real_filename, gotconf['remote'])

        checksum = gotconf['sha-256']
        if not status_local(real_filename, checksum):
            return "Modified: '%s' (remote '%s')" % (real_filename, gotconf['remote'])
        # If we make it here, then the file exists locally and is the same as on
        # the remote.  If it's not present in our cache, then add it

        remote = get_remote(gotconf)
        cache_path = remote.generate_path_for_cache(checksum)

        if os.path.isfile(cache_path):
            return "File already in cache: '%s'" % (real_filename)

        if not remote.store_in_cache(real_filename, checksum):
            return "Failed adding to local cache: '%s'" % (real_filename)
        return "Adding file to local cache: '%s'" % (real_filename)
    except Exception as e:
        raise GotException("Failed to fill cache for '%s': %s" % (real_filename, str(e)))

def file_is_managed_by_git(repo, filename):
    w = repo.get_walker(paths=[filename], max_entries=1)
    try:
        c = iter(w).next().commit
    except StopIteration:
        return False
    else:
        return True

def arguments_are_in_git_repository(args, origpath):
    # We need to check to make sure that the files/directories that the user is
    # trying to do the operation on is in the git repository (otherwise we end
    # up with a corrupted git repo).  We already know our current working
    # directory is the root of the git repository, so we just have to make sure
    # that the current argument starts with a path which is the cwd.  Note that
    # we do this check before any work so we can avoid adding some files but
    # not others.
    for arg in args:
        fullpath = os.path.normpath(os.path.join(origpath, arg))
        if not os.path.realpath(fullpath).startswith(os.getcwd()):
            raise GotException("Argument '%s' is not located in the git repository" % fullpath)

def add_walker(repo, origpath, cb_params, args):
    """
    A function to walk down a list of files/directories, calling the add_cb for
    each argument.

    @param repo         Dulwich repository object to pass into the callback
    @param origpath     The original current working directory when got was
                        invoked, used to figure out the appropriate paths
    @param cb_params    Callback specific parameters to pass to the callback
    @param args         The list of files/directories to walk

    @return A string built from the output of all invocations of the callback
            function.
    """
    arguments_are_in_git_repository(args, origpath)

    output = []

    for arg in args:
        fullpath = os.path.normpath(os.path.join(origpath, arg))
        logging.debug('add_walker: processing argument %s' % fullpath)
        if os.path.isdir(fullpath):
            for base, dirs, filenames in os.walk(fullpath):
                if '.git' in dirs:
                    dirs.remove('.git')
                if '.got' in dirs:
                    dirs.remove('.got')
                for filename in filenames:
                    # When we are recursively adding, we don't want to touch any files
                    # that are already managed by git.
                    if file_is_managed_by_git(repo, os.path.normpath(os.path.join(base, filename))):
                        continue
                    realpath = os.path.normpath(os.path.join(base, filename))
                    gotpath = os.path.normpath(os.path.join(base, "." + filename + ".got"))
                    logging.debug('add_walker: processing file %s' % realpath)
                    output.append(add_cb(repo, gotpath, realpath, cb_params))
        else:
            # this covers both the case where the argument is a file and the
            # case where the full path isn't a file at all (which can happen if
            # the local version of the file was deleted)
            logging.debug('add_walker: processing file %s' % fullpath)
            (base, filename) = os.path.split(fullpath)
            output.append(add_cb(repo, os.path.join(base, '.%s.got' % filename), fullpath, cb_params))
    return output

def walker(function, repo, origpath, cb_params, args):
    """
    A function to walk down a list of files/directories, calling a callback on
    each one.  The callback is expected to have a signature of:

    cb(repo, got_filename, real_filename, cb_params)

    @param function     The function to call on each got managed file
    @param repo         Dulwich repository object to pass into the callback
    @param origpath     The original current working directory when got was
                        invoked, used to figure out the appropriate paths
    @param cb_params    Callback specific parameters to pass to the callback
    @param args         The list of files/directories to walk

    @return A string built from the output of all invocations of the callback
            function.
    """
    output = []

    arguments_are_in_git_repository(args, origpath)

    for arg in args:
        fullpath = os.path.normpath(os.path.join(origpath, arg))
        logging.debug('walker: processing argument %s' % fullpath)
        if os.path.isdir(fullpath):
            for base, dirs, filenames in os.walk(fullpath):
                if '.git' in dirs:
                    dirs.remove('.git')
                if '.got' in dirs:
                    dirs.remove('.got')
                for filename in fnmatch.filter(filenames, '.*.got'):
                    realpath = os.path.normpath(os.path.join(base, filename[1:-4]))
                    gotpath = os.path.normpath(os.path.join(base, filename))
                    logging.debug('walker: processing file %s' % realpath)
                    output.append(function(repo, gotpath, realpath, cb_params))
        else:
            # this covers both the case where the argument is a file and the
            # case where the full path isn't a file at all (which can happen if
            # the local version of the file was deleted)
            logging.debug('walker: processing file %s' % fullpath)
            (base, filename) = os.path.split(fullpath)
            output.append(function(repo, os.path.join(base, '.%s.got' % filename), fullpath, cb_params))
    return output

############################## MAIN HELPERS ##################################
def parse_opts(argv):
    """
    A function to parse the command-line arguments and return the values in
    a tuple.  This function always returns the same tuple; the default value
    is returned for those arguments that were not specified on the command-line.

    @param argv  The arguments passed on the command-line
    @return A tuple of all option values.
    """
    loglevel = logging.ERROR
    logformat = "%(message)s"
    remote = None
    help_requested = False
    verbose = False
    force = False
    recurse = False
    try:
        opts, args = getopt.gnu_getopt(argv[1:], 'd:fhRr:v', ['debug', 'force',
                                                              'help', 'remote',
                                                              'recurse', 'verbose'])
    except getopt.GetoptError as err:
        raise GotException(str(err), need_usage=True)

    for o, a in opts:
        if o in ("-d", "--debug"):
            try:
                d_int = int(a)
            except ValueError:
                raise GotException("", need_usage=True)

            if d_int == 0:
                loglevel = logging.ERROR
            elif d_int == 1:
                loglevel = logging.WARNING
            elif d_int == 2:
                loglevel = logging.INFO
            elif d_int == 3:
                loglevel = logging.DEBUG
            elif d_int >= 4:
                loglevel = logging.DEBUG
                logformat = logging.BASIC_FORMAT
        elif o in ("-f", "--force"):
            force = True
        elif o in ("-h", "--help"):
            help_requested = True
        elif o in ("-R", "--recurse"):
            recurse = True
        elif o in ("-r", "--remote"):
            remote = a
        elif o in ("-v", "--verbose"):
            verbose = True
        else:
            raise GotException("unhandled option '%s'" % o)

    return (args, loglevel, logformat, remote, help_requested, verbose, force, recurse)

def find_git_path_and_chdir():
    """
    This function is expected to be called at the beginning and goes looking
    for the closest .git subdirectory or file in the filesystem hierarchy.
    We look for the .git subdirectory by first looking in ./.git, then changing
    directory to .. and looking for .git, etc, until we either find a .git
    directory or until we hit / (at which point we raise an error).  At the end
    we have changed directory to the appropriate level, which is similar to how
    git itself operates.  Assuming we succeed, this function returns a relative
    path to the original path we started out in so that subsequent operations
    (that may git add) can do the right thing.

    @return The original path that the got command was started in relative to the .git subdirectory.
    """
    try:
        origpath = os.getcwd()
        curpath = origpath
        while not os.path.exists(os.path.join(curpath, '.git')) and curpath != '/':
            os.chdir("..")
            curpath = os.getcwd()

        if curpath == '/':
            raise Exception("Could not find git repository")

        # OK, we found the .git directory/file.  Now return the original path
        # relative to that found directory
        return os.path.relpath(origpath, os.path.commonprefix([origpath, os.getcwd()]))
    except Exception as e:
        raise GotException("Failed to initialize git-got: %s" % (str(e)))

def check_initialized():
    """
    Function to look for got initialization, and open up the configuration if
    found.

    @return True if the got configuration file was found, False otherwise
    """
    global remote_objs
    if os.path.isfile('.got/default'):
        for f in os.listdir(".got"):
            with open(os.path.join('.got', f), 'rb') as storagefp:
                configuration = json.load(storagefp)
            remote_objs.append(RemoteObjFactory(configuration['remote_type'],
                                                configuration['remote'],
                                                configuration))
        return True
    return False

def check_version():
    """
    Function to check to make sure the version of the remote storage files is
    correct.

    @return True if all of the remote storage files are at the current version, False otherwise.
    """
    global remote_objs
    for remote_obj in remote_objs:
        if remote_obj.version() != VERSION:
            return False
    return True

############################# COMMAND IMPLEMENTATIONS ##########################
def add_remote(args, default, repo):
    """
    A function to add a got remote to the repository.  This is called both during
    init and during add_remote time.

    @param args  The command-line arguments
    @param default  Whether this repository is the default one
    @param repo     Dulwich repository object
    """
    if len(args) != 4:
        raise GotException("Not enough arguments to init/add_remote", need_usage=True)

    name = args[1]
    remote_type = args[2]

    # here, make sure we don't blow away an already configured got repository
    if default:
        if os.path.isfile('.got/default'):
            raise GotException("Got remote already initialized!")
        filename = os.path.join('.got', 'default')
    else:
        if not os.path.isfile('.got/default'):
            raise GotException("Got must be initialized before adding additional remotes")
        filename = os.path.join('.got', name)

    # make sure the user doesn't try to add a remote with the same name as an
    # existing remote
    for remote_obj in remote_objs:
        if remote_obj.remote_name() == name:
            raise GotException("Failed to add remote: a remote with the name '%s' already exists" % (name))

    remote = args[3]

    RemoteObjFactory(remote_type, remote, None)

    mkdir_p('.got')
    configuration = { 'remote' : remote , 'remote_type' : remote_type , 'version' : VERSION, 'name': name, 'default': default }
    with open(filename, 'ab') as storagefile:
        json.dump(configuration, storagefile)
    dulwich.porcelain.add(repo, filename)

def upgrade_command(args):
    """
    Run the upgrade command.  This is not yet implemented.

    @param args  The non-option arguments to this command
    """
    if len(args) != 1:
        raise GotException("", need_usage=True)

def add_command(args, recurse, repo, origpath, remote):
    """
    Run the add command to add a file to git-got tracking.  Addition of
    subdirectories is not allowed.

    @param args      The non-option arguments to this command
    @param recurse   Whether to recurse into the directories
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    @param remote    The remote to add this file to (maybe None, which means use the default)
    """
    if len(args) < 2:
        raise GotException("Not enough arguments to add command", need_usage=True)

    if not recurse:
        # we only allow adding files (not subdirectories); check that here
        for arg in args[1:]:
            if os.path.isdir(os.path.join(origpath, arg)):
                raise GotException("Got only allows files, not subdirectories, to be added")

        add_walker(repo, origpath, remote, args[1:])

def reset_command(args, repo, origpath):
    """
    Run the reset command, to reset git-got tracked file(s) to their original
    state.  Reset of subdirectories is not allowed.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) < 2:
        raise GotException("Not enough arguments to reset command", need_usage=True)

    # we only allow resetting files (not subdirectories); check that here
    for arg in args[1:]:
        if os.path.isdir(os.path.join(origpath, arg)):
            raise GotException("Got only allows files, not subdirectories, to be reset")

    walker(reset_cb, repo, origpath, None, args[1:])

def get_command(args, force, repo, origpath):
    """
    Run the get command to fetch git-got tracked file(s) to the local directory.
    If parameters are given, only the given files are fetched.  If no parameters
    are given, all git-got tracked files are fetched.  In both cases, only the
    files that need to be fetched are actually fetched.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) == 1:
        path = ['.']
    elif len(args) > 1:
        path = args[1:]
    else:
        raise GotException("Not enough arguments to get command", need_usage=True)

    walker(get_cb, repo, origpath, force, path)

def status_command(args, repo, origpath, verbose):
    """
    Run the status command to get the status of git-got tracked files.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) == 1:
        path = ['.']
    elif len(args) > 1:
        path = args[1:]
    else:
        raise GotException("Not enough arguments to status command", need_usage=True)

    changes = walker(status_cb, repo, origpath, verbose, path)

    print('# Changes')
    for change in changes:
        if None != change:
            print('# %s' % change)

def rm_command(args, recurse, repo, origpath):
    """
    Run the rm command to remove a file from git-got tracking.  Removal of
    subdirectories is not allowed.

    @param args      The non-option arguments to this command
    @param recurse   Whether to recurse into the directories
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) < 2:
        raise GotException("Not enough arguments to rm command", need_usage=True)

    if not recurse:
        # we only allow removing files (not subdirectories); check that here
        for arg in args[1:]:
            if os.path.isdir(os.path.join(origpath, arg)):
                raise GotException("Got only allows files, not subdirectories, to be removed")

    walker(rm_cb, repo, origpath, None, args[1:])

def remove_remote_command(args, repo, origpath):
    """
    Run the remove_remote command to remove a remote as a possible got backend.
    If there are any files in the project still linked to the remote, the removal
    of the remote will fail.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) != 2:
        raise GotException("", need_usage=True)

    name = args[1]

    remote_obj = None
    for tmp in remote_objs:
        if tmp.remote_name() == name:
            if tmp.remote_default():
                raise GotException("Cannot remove default remote")
            else:
                remote_obj = tmp
                break

    if remote_obj is None:
        raise GotException("Could not find remote named '%s'" % name)

    links = walker(remove_remote_check_cb, repo, origpath, name, ['.'])

    exceptstr = ''
    for link in links:
        if link != '':
            exceptstr += "%s, " % link

    if len(exceptstr) != 0:
        if exceptstr[-2:] == ', ':
            exceptstr = exceptstr[:-2]
        raise GotException("Cannot remove remote '%s'; the following files are linked to it:\n\n  " % name + exceptstr)

    # OK, there are no links to this remote.  We can remove it
    fullpath = os.path.join(".got", remote_obj.remote_name())
    if fullpath in dulwich.porcelain.status(repo).staged['add']:
        # In theory we should use dulwich.porcelain.rm() here, but for some
        # reason it doesn't seem to work.  This is basically the same thing.
        index = repo.open_index()
        del index[fullpath]
        index.write()
        os.remove(fullpath)
    else:
        os.remove(fullpath)
        dulwich.porcelain.add(repo, fullpath)

def list_remotes_command(args):
    """
    Run the list_remotes command to list all remote backends.

    @param args      The non-option arguments to this command
    """
    if len(args) != 1:
        raise GotException("Invalid number of arguments to list_remotes command", need_usage=True)

    print("   Name:\tType:\tURL:")
    print("-" * 80)
    for remote_obj in remote_objs:
        if remote_obj.remote_default():
            print(" *"),
        else:
            print("  "),
        print("%s\t%s\t%s" % (remote_obj.remote_name(), remote_obj.remote_type(), remote_obj.remote_url()))

def chmod_command(args, repo, origpath):
    if len(args) != 3:
        raise GotException("", need_usage=True)

    filename = args[1]
    mode = args[2]
    walker(chmod_cb, repo, origpath, mode, [filename])

def mv_command(args, repo, origpath):
    """
    Run the mv command to move a file in git-got tracking from one name to
    another.  Moving subdirectories is not allowed.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    if len(args) != 3:
        raise GotException("Invalid number of arguments to mv command", need_usage=True)

    # we only allow moving files (not subdirectories); check that here
    if os.path.isdir(os.path.join(origpath, args[1])):
        raise GotException("Got only allows files, not subdirectories, to be moved")

    # Note that going through walker here is a bit of overkill, since we know
    # that we will only have a single argument.  However, we do this so that the
    # mv command goes through all of the same checks as the rest of the commands.
    walker(mv_cb, repo, origpath, args[2], [args[1]])

def rm_local_command(args, repo, origpath):
    """
    Run the rm_local command to remove all local files.  This will cause all of
    the got tracked files to be re-downloaded on the next git got get.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """

    if len(args) != 1:
        raise GotException("Invalid number of arguments to rm_local command", need_usage=True)

    walker(rm_local_cb, repo, origpath, None, ['.'])

def clear_local_cache_command():
    print("Erasing git got local cache")

    if os.path.isdir(local_cache_path):
        shutil.rmtree(local_cache_path)
    if os.path.isdir(local_cache_path):
        raise GotException("Failed to clear cache")
    print("Cleared local cache")

def fill_local_cache_command(args, repo, origpath):
    """
    Run the fill_local_cache command to add to the local cache the files the
    current project already have checked in.

    @param args      The non-option arguments to this command
    @param repo      Dulwich repository object
    @param origpath  The original path that git-got was started in
    """
    print("Filling current cache")

    if len(args) == 1:
        path = ['.']
    elif len(args) > 1:
        path = args[1:]
    else:
        raise GotException("Not enough arguments to status command", need_usage=True)

    changes = walker(fill_local_cache_cb, repo, origpath, [], path)

    print('# Cache status')
    for change in changes:
        if None != change:
            print('# %s' % change)

############################### MAIN ##########################################
def _main(argv):
    loglevel = logging.ERROR
    try:
        (args, loglevel, logformat, remote, help_requested, verbose, force, recurse) = parse_opts(argv)

        if help_requested:
            print(usage())
            return 0

        if len(args) < 1:
            raise GotException("", need_usage=True)

        logging.basicConfig(level=loglevel, format=logformat)

        mkdir_p(local_cache_path)

        origpath = find_git_path_and_chdir()

        repo = dulwich.porcelain.open_repo(".")

        command = args[0]

        if command != 'add' and remote != None:
            # the remote argument only works for add
            raise GotException("", need_usage=True)

        if command != 'status' and verbose:
            # the verbose argument only works for status
            raise GotException("", need_usage=True)

        if command != 'init':
            if not check_initialized():
                raise GotException('Got not initialized',
                                   need_usage=command!='fill-local-cache')

        if command != 'init' and command != 'upgrade':
            if not check_version():
                raise GotException("Version of got repository requires upgrading, run upgrade command", need_usage=True)

        if command == 'init':
            add_remote(args, True, repo)
        elif command == 'upgrade':
            upgrade_command(args)
        elif command == 'add':
            add_command(args, recurse, repo, origpath, remote)
        elif command == 'reset':
            reset_command(args, repo, origpath)
        elif command == 'get':
            get_command(args, force, repo, origpath)
        elif command == 'status':
            status_command(args, repo, origpath, verbose)
        elif command == 'rm':
            rm_command(args, recurse, repo, origpath)
        elif command == "add_remote":
            add_remote(args, False, repo)
        elif command == "remove_remote":
            remove_remote_command(args, repo, origpath)
        elif command == "list_remotes":
            list_remotes_command(args)
        elif command == "chmod":
            chmod_command(args, repo, origpath)
        elif command == "mv":
            mv_command(args, repo, origpath)
        elif command == "rm_local":
            rm_local_command(args, repo, origpath)
        elif command == "clear-local-cache":
            clear_local_cache_command()
        elif command == "fill-local-cache":
            fill_local_cache_command(args, repo, origpath)
        else:
            raise GotException("", need_usage=True)
        return 0
    except Exception as e:
        if loglevel == logging.DEBUG:
            raise
        else:
            print(str(e))
            return 1

def main():
    exit(_main(sys.argv))

if __name__ == "__main__":
    main()

# vim: set filetype=python :
