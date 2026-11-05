#!/usr/bin/env python3
"""Append new Blogger posts to the existing Internet Archive RSS without rewriting old entries."""
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime
from html import unescape

BLOG_ID = '1850249675015842133'
ITEM = 'westbros-complete-rss'
FILENAME = 'westbros-complete-rss.xml'
BASE = f'https://archive.org/download/{ITEM}/{FILENAME}'
ATOM = '{http://www.w3.org/2005/Atom}'
OPENSEARCH = '{http://a9.com/-/spec/opensearchrss/1.0/}'
ET.register_namespace('itunes', 'http://www.itunes.com/dtds/podcast-1.0.dtd')
ET.register_namespace('content', 'http://purl.org/rss/1.0/modules/content/')


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'WestBrosFeedUpdater/1.0', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read()


def entries_from_blogger():
    entries = []
    start = 1
    expected = None
    while True:
        url = f'https://www.blogger.com/feeds/{BLOG_ID}/posts/default?max-results=150&start-index={start}'
        root = ET.fromstring(get(url))
        total = root.findtext(OPENSEARCH + 'totalResults')
        if total is not None:
            expected = int(total)
        batch = root.findall(ATOM + 'entry')
        if not batch:
            break
        entries.extend(batch)
        start += len(batch)
        if len(batch) < 150:
            break
        if start > 10000:
            raise RuntimeError('Unexpectedly large Blogger feed')
    if expected is None or len(entries) != expected or expected < 262:
        raise RuntimeError(f'Incomplete Blogger response: {len(entries)} entries; expected {expected}, minimum 262')
    return entries


def blog_link(entry):
    for link in entry.findall(ATOM + 'link'):
        if link.get('rel') == 'alternate' and link.get('href'):
            return link.get('href')
    return None


def normalize(url):
    return (url or '').split('#', 1)[0].rstrip('/')


def new_rss_item(entry):
    title = entry.findtext(ATOM + 'title') or 'Untitled episode'
    link = blog_link(entry)
    content = entry.findtext(ATOM + 'content') or entry.findtext(ATOM + 'summary') or ''
    # Blogger's HTML includes the episode MP3 link.
    matches = re.findall(r'''(?:https?:)?//[^\s"'<>]+?\.mp3(?:\?[^\s"'<>]*)?''', unescape(content), re.I)
    if not matches:
        raise RuntimeError(f'No MP3 found for new post: {title}')
    mp3 = matches[0]
    if mp3.startswith('//'):
        mp3 = 'https:' + mp3
    published = entry.findtext(ATOM + 'published') or entry.findtext(ATOM + 'updated')
    if not published or not link:
        raise RuntimeError(f'Missing date or link for new post: {title}')
    date = datetime.fromisoformat(published.replace('Z', '+00:00'))
    item = ET.Element('item')
    ET.SubElement(item, 'title').text = title
    ET.SubElement(item, 'link').text = link
    ET.SubElement(item, 'guid', {'isPermaLink': 'false'}).text = entry.findtext(ATOM + 'id') or link
    ET.SubElement(item, 'pubDate').text = format_datetime(date)
    ET.SubElement(item, 'description').text = content
    ET.SubElement(item, 'enclosure', {'url': mp3, 'length': '0', 'type': 'audio/mpeg'})
    return item


def main():
    original = get(BASE + '?nocache=' + str(int(time.time())))
    rss = ET.fromstring(original)
    channel = rss.find('channel')
    if rss.tag != 'rss' or channel is None:
        raise RuntimeError('Internet Archive source is not RSS')
    existing = channel.findall('item')
    if len(existing) < 262:
        raise RuntimeError(f'Archive source has only {len(existing)} items; refusing to update')
    seen_links = {normalize(x.findtext('link')) for x in existing}
    seen_guids = {x.findtext('guid') for x in existing}
    posts = entries_from_blogger()
    additions = []
    for entry in posts:
        link = blog_link(entry)
        guid = entry.findtext(ATOM + 'id')
        if normalize(link) in seen_links or guid in seen_guids:
            continue
        new = new_rss_item(entry)
        additions.append(new)
        seen_links.add(normalize(link))
        seen_guids.add(guid)
    if not additions:
        print(f'No new episodes. Existing: {len(existing)}; Blogger: {len(posts)}')
        return
    # Insert in reverse order at the beginning, keeping newest-first order.
    for item in reversed(additions):
        channel.insert(0, item)
    if len(channel.findall('item')) < len(existing):
        raise RuntimeError('RSS item count decreased; refusing to upload')
    ET.indent(rss, space='  ')
    ET.ElementTree(rss).write(FILENAME, encoding='utf-8', xml_declaration=True)
    access = os.environ.get('IA_ACCESS_KEY')
    secret = os.environ.get('IA_SECRET_KEY')
    if not access or not secret:
        raise RuntimeError('Missing Internet Archive GitHub secrets')
    from internetarchive import get_item, get_session
    session = get_session(config={'s3': {'access': access, 'secret': secret}})
    item = get_item(ITEM, archive_session=session)
    responses = item.upload(FILENAME, overwrite=True, verbose=False)
    for response in responses:
        response.raise_for_status()
    print(f'Uploaded {len(additions)} new episode(s). Total: {len(existing) + len(additions)}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Updater failed safely: {exc}', file=sys.stderr)
        sys.exit(1)
