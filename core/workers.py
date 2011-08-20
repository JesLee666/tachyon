# Tachyon - Fast Multi-Threaded Web Discovery Tool
# Copyright (c) 2011 Gabriel Tremblay - initnull hat gmail.com
#
# GNU General Public Licence (GPL)
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 59 Temple
# Place, Suite 330, Boston, MA  02111-1307  USA
#


import re
import uuid
from core import database, conf, utils
from core.fetcher import Fetcher
from urlparse import urljoin
from threading import Thread
from binascii import crc32

def handle_timeout(queued, url, thread_id):
    """ Handle timeout operation for workers """
    if not queued['timeout_count']:
        queued['timeout_count'] = 0

    if queued.get('timeout_count') < conf.max_timeout_count:
        new_timeout_count = queued.get('timeout_count') + 1
        queued['timeout_count'] = new_timeout_count

        if conf.debug:
            utils.output_info('Thread #' + str(thread_id) + ': re-queuing ' + str(queued))

        # Add back the timed-out item
        database.fetch_queue.put(queued)
    else:
        # We definitely timed out
        utils.output_timeout(url)

def compute_limited_crc(content, length):
    """ Compute the CRC of len bytes, use everything is len(content) is smaller than asked """
    if len(content) < length:
        return crc32(content[0:len(content) - 1]) 
    else:            
        return crc32(content[0:length - 1])


class Compute404CRCWorker(Thread):
    """
    This worker Generate a faked, statistically invalid filename to generate a 404 errror. The CRC32 checksum
    of this error page is then sticked to the path to use it to validate all subsequent request to files under
    that same path.
    """
    def __init__(self, thread_id, display_output=True):
        Thread.__init__(self)
        self.kill_received = False
        self.thread_id = thread_id
        self.fetcher = Fetcher()
        self.display_output = display_output

    def run(self):
        while not self.kill_received:
            # don't wait for any items if empty
            if not database.fetch_queue.empty():
                queued = database.fetch_queue.get()
                random_file = str(uuid.uuid4())
                base_url = queued.get('url') 

                if base_url == '/':
                    url = urljoin(conf.target_host, base_url + random_file)
                else :
                    url = urljoin(conf.target_host, base_url + '/' + random_file)

                if conf.debug:
                    utils.output_debug(str(url))

                # Fetch the target url
                response_code, content, headers = self.fetcher.fetch_url(url, conf.user_agent, conf.fetch_timeout_secs)

                # Handle fetch timeouts by re-adding the url back to the global fetch queue
                # if timeout count is under max timeout count
                if response_code is 0 or response_code is 500:
                    handle_timeout(queued, url, self.thread_id)
                else:
                    # Compute the CRC32 of this url. This is used mainly to validate a fetch against a model 404
                    # All subsequent files that will be joined to those path will use the path crc value since
                    # I think a given 404 will mostly be bound to a directory, and not to a specific file.
                    # This step is only made in initial discovery mode. (Should be moved to a separate worker)
                    queued['computed_404_crc'] = compute_limited_crc(content, conf.crc_sample_len)
    
                    # Exception case for root 404, since it's used as a model for other directories
                    if queued.get('url') == '/':
                        database.root_404_crc = queued['computed_404_crc']
                         
                    # The path is then added back to a validated list
                    database.valid_paths.append(queued)
    
                    if conf.debug:
                        utils.output_debug("Computed Checksum for: " + str(queued))
    
                    # We are done
                    database.fetch_queue.task_done()    



class TestUrlExistsWorker(Thread):
    """ This worker get an url from the work queue and call the url fetcher """
    def __init__(self, thread_id, display_output=True):
        Thread.__init__(self)
        self.kill_received = False
        self.thread_id = thread_id
        self.fetcher = Fetcher()
        self.display_output = display_output

    def run(self):
        while not self.kill_received:
            # don't wait for any items if empty
            if not database.fetch_queue.empty():
                queued = database.fetch_queue.get()
                url = urljoin(conf.target_host, queued.get('url'))
                description = queued.get('description')
                match_string = queued.get('match_string')
                #computed_404_crc = queued.get('computed_404_crc')

                # don't test '/' for existence :)
                if queued.get('url') == '/':
                    database.fetch_queue.task_done()
                    continue

                if conf.debug:
                    utils.output_debug("Testing: " + url)

                # Fetch the target url
                response_code, content, headers = self.fetcher.fetch_url(url, conf.user_agent, conf.fetch_timeout_secs)

                # handle timeout
                if response_code is 0 or response_code is 500:
                    handle_timeout(queued, url, self.thread_id)
                else:
                    # Test classic html response code
                    if response_code in conf.expected_path_responses:
                        # At this point each directory should have had his 404 crc computed (tachyon main loop)
                        crc = compute_limited_crc(content, conf.crc_sample_len)
                        
                        # If the CRC missmatch, and we have an expected code, we found a valid link
                        if crc != database.root_404_crc:
                            if response_code == 401:
                                # Output result, but don't keep the url since we can't poke in protected folder
                                if self.display_output or conf.debug:
                                    utils.output_found('*Password Protected* ' + description + ' at: ' + url)
                            else:
                                # Content Test if match_string provided
                                if match_string:
                                    if re.search(re.escape(match_string), content, re.I):
                                        # Add path to valid_path for future actions
                                        database.valid_paths.append(queued)
                                        if self.display_output or conf.debug:
                                            utils.output_found("String-Matched " + description + ' at: ' + url)
                                else:
                                    # Add path to valid_path for future actions
                                    database.valid_paths.append(queued)
                                    if self.display_output or conf.debug:
                                        utils.output_found(description + ' at: ' + url)

                # Mark item as processed
                database.fetch_queue.task_done()


class PrintWorker(Thread):
    """ This worker is used to generate a synchronized non-overlapping console output. """

    def __init__(self):
        Thread.__init__(self)
        self.kill_received = False

    def run(self):
        while not self.kill_received:
            text = database.output_queue.get()
            print text
            database.output_queue.task_done()
