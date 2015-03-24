#!/usr/bin/env python
# -*- coding: UTF-8 -*-

# Copyright (c) 2015, Matthew Brennan Jones <matthew.brennan.jones@gmail.com>
# Copyright (C) 2013-2014 Barnaby Gale
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies of the Software and its documentation and acknowledgment shall be
# given in the documentation and software packages that this Software was
# used.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


import sys
import urllib
import struct
import datetime

PY2 = (sys.version_info[0] == 2)


if PY2:
	try:
		from cStringIO import StringIO
	except ImportError:
		from StringIO import StringIO
else:
	from io import BytesIO

SECTOR_SIZE = 2048

class ISO9660IOError(IOError):
    def __init__(self, path):
        self.path = path

    def __str__(self):
        return "Path not found: {0}".format(self.path)

class ISO9660(object):
    def __init__(self, url):
        self._buff  = None #input buffer
        self._root  = None #root node
        self._pvd   = {}   #primary volume descriptor
        self._paths = []   #path table

        self._url   = url
        if not hasattr(self, '_get_sector'): #it might have been set by a subclass
            self._get_sector = self._get_sector_url if url.startswith('http') else self._get_sector_file

        ### Volume Descriptors
        sector = 0x10
        while True:
            self._get_sector(sector, SECTOR_SIZE)
            sector += 1
            ty = self._unpack('B')

            if ty == 1:
                self._unpack_pvd()
            elif ty == 255:
                break
            else:
                continue

        ### Path table
        l0 = self._pvd['path_table_size']
        self._get_sector(self._pvd['path_table_l_loc'], l0)

        while l0 > 0:
            p = {}
            l1 = self._unpack('B')
            l2 = self._unpack('B')
            p['ex_loc'] = self._unpack('<I')
            p['parent'] = self._unpack('<H')
            p['name']   = self._unpack_string(l1)
            if p['name'] == b'\x00':
                p['name'] = b''

            if l1%2 == 1:
                self._unpack('B')

            self._paths.append(p)

            l0 -= 8 + l1 + (l1 % 2)

        assert l0 == 0

    ##
    ## Generator listing available files/folders
    ##

    def tree(self, get_files = True):
        if get_files:
            gen = self._tree_node(self._root)
        else:
            gen = self._tree_path(b'', 1)

        yield b'/'
        for i in gen:
            yield i

    def _tree_path(self, name, index):
        spacer = lambda s: name + b"/" + s
        for i, c in enumerate(self._paths):
            if c['parent'] == index and i != 0:
                yield spacer(c['name'])
                for d in self._tree_path(spacer(c['name']), i+1):
                    yield d

    def _tree_node(self, node):
        spacer = lambda s: node['name'] + b"/" + s
        for c in list(self._unpack_dir_children(node)):
            yield spacer(c['name'])
            if c['flags'] & 2:
                for d in self._tree_node(c):
                    yield spacer(d)

    ##
    ## Retrieve file contents as a string
    ##

    def get_file(self, path):
        path = path.upper().strip(b'/').split(b'/')
        path, filename = path[:-1], path[-1]

        if len(path)==0:
            parent_dir = self._root
        else:
            try:
                parent_dir = self._dir_record_by_table(path)
            except ISO9660IOError:
                parent_dir = self._dir_record_by_root(path)

        f = self._search_dir_children(parent_dir, filename)

        self._get_sector(f['ex_loc'], f['ex_len'])
        return self._unpack_raw(f['ex_len'])

    ##
    ## Methods for retrieving partial contents
    ##

    def _get_sector_url(self, sector, length):
        start = sector * SECTOR_SIZE
        if self._buff:
            self._buff.close()
        opener = urllib.FancyURLopener()
        opener.http_error_206 = lambda *a, **k: None
        opener.addheader(b"Range", b"bytes={0}-{1}".format(start, start+length-1))
        self._buff = opener.open(self._url)

    def _get_sector_file(self, sector, length):
        with open(self._url, 'rb') as f:
            f.seek(sector*SECTOR_SIZE)
            self._buff = BytesIO(f.read(length))

    ##
    ## Return the record for final directory in a path
    ##

    def _dir_record_by_table(self, path):
        for e in self._paths[::-1]:
            search = list(path)
            f = e
            while f['name'] == search[-1]:
                search.pop()
                f = self._paths[f['parent']-1]
                if f['parent'] == 1:
                    e['ex_len'] = SECTOR_SIZE #TODO
                    return e

        raise ISO9660IOError(path)

    def _dir_record_by_root(self, path):
        current = self._root
        remaining = list(path)

        while remaining:
            current = self._search_dir_children(current, remaining[0])

            remaining.pop(0)

        return current

    ##
    ## Unpack the Primary Volume Descriptor
    ##

    def _unpack_pvd(self):
        self._pvd['type_code']                     = self._unpack_string(5)
        self._pvd['standard_identifier']           = self._unpack('B')
        self._unpack_raw(1)                        #discard 1 byte
        self._pvd['system_identifier']             = self._unpack_string(32)
        self._pvd['volume_identifier']             = self._unpack_string(32)
        self._unpack_raw(8)                        #discard 8 bytes
        self._pvd['volume_space_size']             = self._unpack_both('i')
        self._unpack_raw(32)                       #discard 32 bytes
        self._pvd['volume_set_size']               = self._unpack_both('h')
        self._pvd['volume_seq_num']                = self._unpack_both('h')
        self._pvd['logical_block_size']            = self._unpack_both('h')
        self._pvd['path_table_size']               = self._unpack_both('i')
        self._pvd['path_table_l_loc']              = self._unpack('<i')
        self._pvd['path_table_opt_l_loc']          = self._unpack('<i')
        self._pvd['path_table_m_loc']              = self._unpack('>i')
        self._pvd['path_table_opt_m_loc']          = self._unpack('>i')
        _, self._root = self._unpack_record()      #root directory record
        self._pvd['volume_set_identifer']          = self._unpack_string(128)
        self._pvd['publisher_identifier']          = self._unpack_string(128)
        self._pvd['data_preparer_identifier']      = self._unpack_string(128)
        self._pvd['application_identifier']        = self._unpack_string(128)
        self._pvd['copyright_file_identifier']     = self._unpack_string(38)
        self._pvd['abstract_file_identifier']      = self._unpack_string(36)
        self._pvd['bibliographic_file_identifier'] = self._unpack_string(37)
        self._pvd['volume_datetime_created']       = self._unpack_vd_datetime()
        self._pvd['volume_datetime_modified']      = self._unpack_vd_datetime()
        self._pvd['volume_datetime_expires']       = self._unpack_vd_datetime()
        self._pvd['volume_datetime_effective']     = self._unpack_vd_datetime()
        self._pvd['file_structure_version']        = self._unpack('B')

    ##
    ## Unpack a directory record (a listing of a file or folder)
    ##

    def _unpack_record(self, read=0):
        l0 = self._unpack('B')

        if l0 == 0:
            return read+1, None

        l1 = self._unpack('B')

        d = dict()
        d['ex_loc']               = self._unpack_both('I')
        d['ex_len']               = self._unpack_both('I')
        d['datetime']             = self._unpack_dir_datetime()
        d['flags']                = self._unpack('B')
        d['interleave_unit_size'] = self._unpack('B')
        d['interleave_gap_size']  = self._unpack('B')
        d['volume_sequence']      = self._unpack_both('h')

        l2 = self._unpack('B')
        d['name'] = self._unpack_string(l2).split(b';')[0]
        if d['name'] == b'\x00':
            d['name'] = b''

        if l2 % 2 == 0:
            self._unpack('B')

        t = 34 + l2 - (l2 % 2)

        e = l0-t
        if e>0:
            extra = self._unpack_raw(e)

        return read+l0, d

    #Assuming d is a directory record, this generator yields its children
    def _unpack_dir_children(self, d):
        sector = d['ex_loc']
        read = 0
        self._get_sector(sector, 2048)

        read, r_self = self._unpack_record(read)
        read, r_parent = self._unpack_record(read)

        while read < r_self['ex_len']: #Iterate over files in the directory
            if read % 2048 == 0:
                sector += 1
                self._get_sector(sector, 2048)
            read, data = self._unpack_record(read)

            if data == None: #end of directory listing
                to_read = 2048 - (read % 2048)
                self._unpack_raw(to_read)
                read += to_read
            else:
                yield data

    #Search for one child amongst the children
    def _search_dir_children(self, d, term):
        for e in self._unpack_dir_children(d):
            if e['name'] == term:
                return e

        raise ISO9660IOError(term)
    ##
    ## Datatypes
    ##

    def _unpack_raw(self, l):
        return self._buff.read(l)

    #both-endian
    def _unpack_both(self, st):
        a = self._unpack('<'+st)
        b = self._unpack('>'+st)
        assert a == b
        return a

    def _unpack_string(self, l):
        return self._buff.read(l).rstrip(b' ')

    def _unpack(self, st):
        if st[0] not in ('<','>'):
            st = '<' + st
        d = struct.unpack(st, self._buff.read(struct.calcsize(st)))
        if len(st) == 2:
            return d[0]
        else:
            return d

    def _unpack_vd_datetime(self):
        return self._unpack_raw(17) #TODO

    def _unpack_dir_datetime(self):
        epoch = datetime.datetime(1970, 1, 1)
        date = self._unpack_raw(7)
        t = []
        date_sub = date[:-1]
        for i in range(len(date_sub)):
            n = date_sub[i : i + 1]
            t.append(struct.unpack('<B', n)[0])
        t.append(struct.unpack('<b', date[-1 : ])[0])
        t[0] += 1900
        t_offset = t.pop(-1) * 15 * 60.    # Offset from GMT in 15min intervals, converted to secs
        t_timestamp = (datetime.datetime(*t) - epoch).total_seconds() - t_offset
        t_datetime = datetime.datetime.fromtimestamp(t_timestamp)
        t_readable = t_datetime.strftime('%Y-%m-%d %H:%M:%S')
        return t_readable



if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("usage: python iso9660.py isourl [path]")
    else:
        iso_path = sys.argv[1]
        ret_path = sys.argv[2] if len(sys.argv) > 2 else None
        cd = ISO9660(iso_path)
        if ret_path:
            sys.stdout.write(cd.get_file(ret_path))
        else:
            for path in cd.tree():
                print(path)
