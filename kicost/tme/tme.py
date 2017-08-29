# MIT license
#
# Copyright (C) 2015 by XESS Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# Inserted by Pasteurize tool.
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from builtins import zip
from builtins import range
from builtins import int
from builtins import str
from future import standard_library
standard_library.install_aliases()

import future

import re
import difflib
import logging
import requests
from bs4 import BeautifulSoup
import http.client # For web scraping exceptions.

try:
    from urllib.parse import urlencode, quote as urlquote, urlsplit, urlunsplit
    import urllib.request
    from urllib.request import urlopen, Request
except ImportError:
    from urlparse import quote as urlquote, urlsplit, urlunsplit
    from urllib import urlencode
    from urllib2 import urlopen, Request

from ..kicost import PartHtmlError, FakeBrowser
from ..kicost import logger, DEBUG_OVERVIEW, DEBUG_DETAILED, DEBUG_OBSESSIVE

from currency_converter import CurrencyConverter

SEPRTR = ':'  # Delimiter between library:component, distributor:field, etc.

HTML_RESPONSE_RETRIES = 2 # Num of retries for getting part data web page.

WEB_SCRAPE_EXCEPTIONS = (urllib.request.URLError, http.client.HTTPException)

from ..kicost import distributors
distributors.update(
    {
        'tme': {
            'scrape': 'web',
            'function': 'tme',
            'label': 'TME',
            'order_cols': ['part_num', 'purch', 'refs'],
            'order_delimiter': ' '
        }
    }
)

def __ajax_details(pn):
    payload = {
        'symbol': pn,
        'currency': 'USD'
    }
    # TODO: Use a different method so that the requests module is not needed
    r = requests.post(
            url='http://www.tme.eu/en/_ajax/ProductInformationPage/_getStocks.html',
            data=payload,
            headers={
               'X-Requested-With': 'XMLHttpRequest'
            })
    # TODO: What if json() fails?
    # TODO: What if json array does not contain required fields?
    j = r.json()
    html_tree = BeautifulSoup(j['Products'][0]['PriceTpl'].replace("\n", ""), "lxml")
    quantity = j['Products'][0]['InStock']
    return html_tree, quantity;

def get_tme_price_tiers(html_tree):
    '''Get the pricing tiers from the parsed tree of the TME product page.'''
    price_tiers = {}
    try:
        pn = get_tme_part_num(html_tree)
        if pn == '':
            return price_tiers

        html_tree, quantity = __ajax_details(pn)

        qty_strs = []
        price_strs = []
        for tr in html_tree.find('tbody', id='prices_body').find_all('tr'):
            # TODO: What if [0] or [2] do not exist?
            td = tr.find_all('td')
            qty_strs.append(td[0].text)
            price_strs.append(td[2].text)
        qtys_prices = list(zip(qty_strs, price_strs))
        for qty_str, price_str in qtys_prices:
            try:
                qty = re.search('(\s*)([0-9,]+)', qty_str).group(2)
                qty = int(re.sub('[^0-9]', '', qty))
                price_tiers[qty] = float(re.sub('[^0-9\.]', '', price_str))
            except (TypeError, AttributeError, ValueError, IndexError):
                continue
    except AttributeError:
        # This happens when no pricing info is found in the tree.
        logger.log(DEBUG_OBSESSIVE, 'No TME pricing information found!')
        return price_tiers  # Return empty price tiers.
    return price_tiers


def get_tme_part_num(html_tree):
    '''Get the part number from the TME product page.'''
    try:
        #tr = html_tree.find('div', class_="col-md-9 symbols").find_all('tr')
        # TODO: Exception when [1] is not found?
        #return re.sub('\n', '', tr[1].find('td', class_='value').text)
        return html_tree.find('td', class_="pip-product-symbol").text
    except AttributeError:
        logger.log(DEBUG_OBSESSIVE, 'No TME part number found!')
        return ''


def get_tme_qty_avail(html_tree):
    '''Get the available quantity of the part from the TME product page.'''
    pn = get_tme_part_num(html_tree)
    if pn == '':
        logger.log(DEBUG_OBSESSIVE, 'No TME part quantity found!')
        return None

    ajax_tree, qty_str = __ajax_details(pn)

    try:
        return int(qty_str)
    except ValueError:
        # No quantity found (not even 0) so this is probably a non-stocked part.
        # Return None so the part won't show in the spreadsheet for this dist.
        logger.log(DEBUG_OBSESSIVE, 'No TME part quantity found!')
        return None


def get_tme_part_html_tree(dist, pn, extra_search_terms='', url=None, descend=2, local_part_html=None):
    '''Find the TME HTML page for a part number and return the URL and parse tree.'''

    # Use the part number to lookup the part using the site search function, unless a starting url was given.
    if url is None:
        url = 'http://www.tme.eu/en/katalog/?search=' + urlquote(
            pn + ' ' + extra_search_terms,
            safe='')
    elif url[0] == '/':
        url = 'http://www.tme.eu' + url

    # Open the URL, read the HTML from it, and parse it into a tree structure.
    req = FakeBrowser(url)
    for _ in range(HTML_RESPONSE_RETRIES):
        try:
            response = urlopen(req)
            html = response.read()
            break
        except WEB_SCRAPE_EXCEPTIONS:
            logger.log(DEBUG_DETAILED,'Exception while web-scraping {} from {}'.format(pn, dist))
            pass
    else: # Couldn't get a good read from the website.
        logger.log(DEBUG_OBSESSIVE,'No HTML page for {} from {}'.format(pn, dist))
        raise PartHtmlError

    # Abort if the part number isn't in the HTML somewhere.
    # (Only use the numbers and letters to compare PN to HTML.)
    if re.sub('[\W_]','',str.lower(pn)) not in re.sub('[\W_]','',str.lower(str(html))):
        logger.log(DEBUG_OBSESSIVE,'No part number {} in HTML page from {} ({})'.format(pn, dist, url))
        raise PartHtmlError

    try:
        tree = BeautifulSoup(html, 'lxml')
    except Exception:
        logger.log(DEBUG_OBSESSIVE,'No HTML tree for {} from {}'.format(pn, dist))
        raise PartHtmlError

    # If the tree contains the tag for a product page, then just return it.
    if tree.find('div', id='ph') is not None:
        return tree, url

    # If the tree is for a list of products, then examine the links to try to find the part number.
    if tree.find('table', id="products") is not None:
        logger.log(DEBUG_OBSESSIVE,'Found product table for {} from {}'.format(pn, dist))
        if descend <= 0:
            logger.log(DEBUG_OBSESSIVE,'Passed descent limit for {} from {}'.format(pn, dist))
            raise PartHtmlError
        else:
            # Look for the table of products.
            products = tree.find(
                'table',
                id="products").find_all(
                    'tr',
                    class_=('product-row'))

            # Extract the product links for the part numbers from the table.
            # TODO: What if [1] does not exist?
            product_links = [p.find('div', class_='manufacturer').find_all('a')[1] for p in products]

            # Extract all the part numbers from the text portion of the links.
            part_numbers = [l.text for l in product_links]

            # Look for the part number in the list that most closely matches the requested part number.
            match = difflib.get_close_matches(pn, part_numbers, 1, 0.0)[0]

            # Now look for the link that goes with the closest matching part number.
            for l in product_links:
                if (not l['href'].startswith('./katalog')) and l.text == match:
                    # Get the tree for the linked-to page and return that.
                    logger.log(DEBUG_OBSESSIVE,'Selecting {} from product table for {} from {}'.format(l.text, pn, dist))
                    return get_tme_part_html_tree(dist, pn, extra_search_terms,
                                                  url=l['href'],
                                                  descend=descend-1)

    # I don't know what happened here, so give up.
    logger.log(DEBUG_OBSESSIVE,'Unknown error for {} from {}'.format(pn, dist))
    raise PartHtmlError
