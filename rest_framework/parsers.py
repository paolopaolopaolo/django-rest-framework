"""
Parsers are used to parse the content of incoming HTTP requests.

They give us a generic way of being able to handle various media types
on the request, such as form content or json encoded data.
"""
from __future__ import unicode_literals

import json
import cStringIO
import base64
import uuid
import urllib
import re
import os

from django.conf import settings
from django.core.files.uploadhandler import StopFutureHandlers
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.http import QueryDict
from django.http.multipartparser import \
    MultiPartParser as DjangoMultiPartParser
from django.http.multipartparser import (
    ChunkIter, MultiPartParserError, parse_header
)
from django.utils import six
from django.utils.encoding import force_text
from django.utils.six.moves.urllib import parse as urlparse

from rest_framework import renderers
from rest_framework.exceptions import ParseError


class DataAndFiles(object):
    def __init__(self, data, files):
        self.data = data
        self.files = files


class BaseParser(object):
    """
    All parsers should extend `BaseParser`, specifying a `media_type`
    attribute, and overriding the `.parse()` method.
    """

    media_type = None

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Given a stream to read from, return the parsed representation.
        Should return parsed data, or a `DataAndFiles` object consisting of the
        parsed data and files.
        """
        raise NotImplementedError(".parse() must be overridden.")


class JSONParser(BaseParser):
    """
    Parses JSON-serialized data.
    """

    media_type = 'application/json'
    renderer_class = renderers.JSONRenderer

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as JSON and returns the resulting data.
        """
        parser_context = parser_context or {}
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)

        try:
            data = stream.read().decode(encoding)
            return json.loads(data)
        except ValueError as exc:
            raise ParseError('JSON parse error - %s' % six.text_type(exc))


class FormParser(BaseParser):
    """
    Parser for form data.
    """

    media_type = 'application/x-www-form-urlencoded'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as a URL encoded form,
        and returns the resulting QueryDict.
        """
        parser_context = parser_context or {}
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        data = QueryDict(stream.read(), encoding=encoding)
        return data


class MultiPartParser(BaseParser):
    """
    Parser for multipart form data, which may include file data.
    """

    media_type = 'multipart/form-data'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as a multipart encoded form,
        and returns a DataAndFiles object.

        `.data` will be a `QueryDict` containing all the form parameters.
        `.files` will be a `QueryDict` containing all the form files.
        """
        parser_context = parser_context or {}
        request = parser_context['request']
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        meta = request.META.copy()
        meta['CONTENT_TYPE'] = media_type
        upload_handlers = request.upload_handlers

        try:
            parser = DjangoMultiPartParser(meta,
                                           stream,
                                           upload_handlers,
                                           encoding)
            data, files = parser.parse()
            return DataAndFiles(data, files)
        except MultiPartParserError as exc:
            raise ParseError('Multipart form parse error - %s' % six.text_type(exc))


class FileUploadParser(BaseParser):
    """
    Parser for file upload data.
    """
    media_type = '*/*'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Treats the incoming bytestream as a raw file upload and returns
        a `DateAndFiles` object.

        `.data` will be None (we expect request body to be a file content).
        `.files` will be a `QueryDict` containing one 'file' element.
        """

        parser_context = parser_context or {}
        request = parser_context['request']
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        meta = request.META
        upload_handlers = request.upload_handlers
        filename = self.get_filename(stream, media_type, parser_context)

        # Note that this code is extracted from Django's handling of
        # file uploads in MultiPartParser.
        content_type = meta.get('HTTP_CONTENT_TYPE',
                                meta.get('CONTENT_TYPE', ''))
        try:
            content_length = int(meta.get('HTTP_CONTENT_LENGTH',
                                          meta.get('CONTENT_LENGTH', 0)))
        except (ValueError, TypeError):
            content_length = None

        # See if the handler will want to take care of the parsing.
        for handler in upload_handlers:
            result = handler.handle_raw_input(None,
                                              meta,
                                              content_length,
                                              None,
                                              encoding)
            if result is not None:
                return DataAndFiles({}, {'file': result[1]})

        # This is the standard case.
        possible_sizes = [x.chunk_size for x in upload_handlers if x.chunk_size]
        chunk_size = min([2 ** 31 - 4] + possible_sizes)
        chunks = ChunkIter(stream, chunk_size)
        counters = [0] * len(upload_handlers)

        for index, handler in enumerate(upload_handlers):
            try:
                handler.new_file(None, filename, content_type,
                                 content_length, encoding)
            except StopFutureHandlers:
                upload_handlers = upload_handlers[:index + 1]
                break

        for chunk in chunks:
            for index, handler in enumerate(upload_handlers):
                chunk_length = len(chunk)
                chunk = handler.receive_data_chunk(chunk, counters[index])
                counters[index] += chunk_length
                if chunk is None:
                    break

        for index, handler in enumerate(upload_handlers):
            file_obj = handler.file_complete(counters[index])
            if file_obj:
                return DataAndFiles({}, {'file': file_obj})
        raise ParseError("FileUpload parse error - "
                         "none of upload handlers can handle the stream")

    def get_filename(self, stream, media_type, parser_context):
        """
        Detects the uploaded file name. First searches a 'filename' url kwarg.
        Then tries to parse Content-Disposition header.
        """
        try:
            return parser_context['kwargs']['filename']
        except KeyError:
            pass

        try:
            meta = parser_context['request'].META
            disposition = parse_header(meta['HTTP_CONTENT_DISPOSITION'].encode('utf-8'))
            filename_parm = disposition[1]
            if 'filename*' in filename_parm:
                return self.get_encoded_filename(filename_parm)
            return force_text(filename_parm['filename'])
        except (AttributeError, KeyError, ValueError):
            pass

    def get_encoded_filename(self, filename_parm):
        """
        Handle encoded filenames per RFC6266. See also:
        http://tools.ietf.org/html/rfc2231#section-4
        """
        encoded_filename = force_text(filename_parm['filename*'])
        try:
            charset, lang, filename = encoded_filename.split('\'', 2)
            filename = urlparse.unquote(filename)
        except (ValueError, LookupError):
            filename = force_text(filename_parm['filename'])
        return filename


class ImageUrlParser(BaseParser):
    """
    Parser for uploading images through URL strings in application/json.
    JSON data must have an attribute called "url_keys", which is an array
    of strings that identify which keys are image urls.
    """

    media_type = "application/json"

    def generate_uuid_filename(self):
        return str(uuid.uuid4())

    def handle_image(self, string_url):
        """
        Takes a url string and converts it into a Django InMemoryUploadedFile
        """

        """
            Assumes URL is http-based, otherwise apply base64 decoding.
            If the latter, save image as PNG
        """
        if re.search(r'http', string_url) is not None:
            file_item = cStringIO.StringIO(urllib.urlopen(string_url).read())
            image_format = re.search(r".([A-Za-z0-9]*)$", string_url).group(1)
        else:
            file_item = cStringIO.StringIO(base64.b64decode(string_url))
            image_format = "png"
        """
            Sets size of file
        """
        file_item.seek(0, os.SEEK_END)
        size = file_item.tell()
        file_item.seek(0)
        """
            Creates InMemoryUploadedFile
        """
        image = InMemoryUploadedFile(
            file_item,
            None,
            ".".join([self.generate_uuid_filename(), image_format]),
            "/".join(['image', image_format]),
            size,
            None)
        return image

    def parse(self, stream, media_type, parser_context):
        """
            Parses url keys in request,
            resetting them to InMemoryUploadedFile
        """
        request = json.loads(stream.read())
        dest_request = request.copy()
        url_keys = request['url_keys']

        for url_key in url_keys:
            if url_key in request:
                dest_request[url_key] = self.handle_image(request[url_key])

        return dest_request