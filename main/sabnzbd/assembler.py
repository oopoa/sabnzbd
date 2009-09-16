#!/usr/bin/python -OO
# Copyright 2008-2009 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.assembler - threaded assembly/decoding of files
"""

import sys
import os
import Queue
import binascii
import logging
import struct
from threading import Thread
from time import sleep
import subprocess
try:
    import hashlib
    new_md5 = hashlib.md5
except:
    import md5
    new_md5 = md5.new

import sabnzbd
from sabnzbd.misc import get_filepath, sanitize_filename, get_unique_path
import sabnzbd.cfg as cfg
import sabnzbd.articlecache
import sabnzbd.postproc
import sabnzbd.downloader
from sabnzbd.lang import T


#------------------------------------------------------------------------------
# Wrapper functions

__ASM = None  # Global pointer to post-proc instance

def init():
    global __ASM
    if __ASM:
        __ASM.__init__(__ASM.queue)
    else:
        __ASM = Assembler()

def start():
    global __ASM
    if __ASM: __ASM.start()


def process(nzf):
    global __ASM
    if __ASM: __ASM.process(nzf)

def stop():
    global __ASM
    if __ASM:
        __ASM.stop()
        try:
            __ASM.join()
        except:
            pass


#------------------------------------------------------------------------------
class Assembler(Thread):
    def __init__ (self, queue = None):
        Thread.__init__(self)

        if queue:
            self.queue = queue
        else:
            self.queue = Queue.Queue()

    def stop(self):
        self.process(None)

    def process(self, nzf):
        self.queue.put(nzf)

    def run(self):
        while 1:
            nzo_nzf_tuple = self.queue.get()
            if not nzo_nzf_tuple:
                logging.info("Shutting down")
                break

            nzo, nzf = nzo_nzf_tuple

            if nzf:
                sabnzbd.CheckFreeSpace()
                filename = sanitize_filename(nzf.get_filename())
                nzf.set_filename(filename)

                dupe = nzo.check_for_dupe(nzf)

                filepath = get_filepath(cfg.DOWNLOAD_DIR.get_path(), nzo, filename)

                if filepath:
                    logging.info('Decoding %s %s', filepath, nzf.get_type())
                    try:
                        filepath = _assemble(nzo, nzf, filepath, dupe)
                    except IOError, (errno, strerror):
                        # 28 == disk full => pause downloader
                        if errno == 28:
                            logging.error(T('error-diskFull'))
                            sabnzbd.downloader.pause_downloader()
                        else:
                            logging.error(T('error-diskError@1'), filepath)

                    setname = nzf.get_setname()
                    if nzf.is_par2() and (nzo.get_md5pack(setname) is None):
                        nzo.set_md5pack(setname, GetMD5Hashes(filepath))
                        logging.debug('Got md5pack for set %s', setname)


            else:
                sabnzbd.postproc.process(nzo)


def _assemble(nzo, nzf, path, dupe):
    if os.path.exists(path):
        unique_path = get_unique_path(path, create_dir = False)
        if dupe:
            path = unique_path
        else:
            os.rename(path, unique_path)

    fout = open(path, 'ab')

    if cfg.QUICK_CHECK.get():
        md5 = new_md5()
    else:
        md5 = None

    _type = nzf.get_type()
    decodetable = nzf.get_decodetable()

    for articlenum in decodetable:
        sleep(0.01)
        article = decodetable[articlenum]

        data = sabnzbd.articlecache.method.load_article(article)

        if not data:
            logging.warning(T('warn-artMissing@1'), article)
        else:
            # yenc data already decoded, flush it out
            if _type == 'yenc':
                fout.write(data)
                if md5: md5.update(data)
            # need to decode uu data now
            elif _type == 'uu':
                data = data.split('\r\n')

                chunks = []
                for line in data:
                    if not line:
                        continue

                    if line == '-- ' or line.startswith('Posted via '):
                        continue
                    try:
                        tmpdata = binascii.a2b_uu(line)
                        chunks.append(tmpdata)
                    except binascii.Error, msg:
                        ## Workaround for broken uuencoders by
                        ##/Fredrik Lundh
                        nbytes = (((ord(line[0])-32) & 63) * 4 + 5) / 3
                        try:
                            tmpdata = binascii.a2b_uu(line[:nbytes])
                            chunks.append(tmpdata)
                        except binascii.Error, msg:
                            logging.info('Decode failed in part %s: %s', article.article, msg)
                fout.write(''.join(chunks))
                if md5: md5.update(''.join(chunks))

    fout.flush()
    fout.close()
    if md5:
        nzf.md5sum = md5.digest()
        del md5

    return path


# For a full description of the par2 specification, visit:
# http://parchive.sourceforge.net/docs/specifications/parity-volume-spec/article-spec.html

def GetMD5Hashes(name):
    """ Get the hash table from a PAR2 file
        Return as dictionary, indexed on names
    """
    table = {}
    try:
        f = open(name, 'rb')
    except:
        return table

    header = f.read(8)
    while header:
        name, hash = ParseFilePacket(f, header)
        if name:
            table[name] = hash
        header = f.read(8)

    f.close()
    return table


def ParseFilePacket(f, header):
    """ Look up and analyse a FileDesc package """

    def ToInt(buf):
        return struct.unpack('<Q', buf)[0]

    nothing = None, None

    if header != 'PAR2\0PKT':
        return nothing

    # Length must be multiple of 4 and at least 20
    len = ToInt(f.read(8))
    if int(len/4)*4 != len or len < 20:
        return nothing

    # Next 16 bytes is md5sum of this packet
    md5sum = f.read(16)

    # Read and check the data
    data = f.read(len-32)
    md5 = new_md5()
    md5.update(data)
    if md5sum != md5.digest():
        return nothing

    # The FileDesc packet looks like:
    # 16 : "PAR 2.0\0FileDesc"
    # 16 : FileId
    # 16 : Hash for full file **
    # 16 : Hash for first 16K
    #  8 : File length
    # xx : Name (multiple of 4, padded with \0 if needed) **

    # See if it's the right packet and get name + hash
    for offset in range(0, len, 8):
        if data[offset:offset+16] == "PAR 2.0\0FileDesc":
            hash = data[offset+32:offset+48]
            filename = data[offset+72:].strip('\0')
            return filename, hash

    return nothing
