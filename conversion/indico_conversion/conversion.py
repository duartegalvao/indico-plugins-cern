# This file is part of the CERN Indico plugins.
# Copyright (C) 2014 - 2022 CERN
#
# The CERN Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License; see
# the LICENSE file for more details.

import os
from datetime import timedelta

import requests
from celery.exceptions import MaxRetriesExceededError, Retry
from flask import jsonify, request, session
from itsdangerous import BadData

from indico.core import signals
from indico.core.celery import celery
from indico.core.config import config
from indico.core.db import db
from indico.core.plugins import url_for_plugin
from indico.modules.attachments.models.attachments import Attachment, AttachmentFile, AttachmentType
from indico.util.fs import secure_filename
from indico.util.signing import secure_serializer
from indico.web.flask.templating import get_template_module
from indico.web.rh import RH

from indico_conversion import pdf_state_cache
from indico_conversion.util import get_pdf_title


MAX_TRIES = 20
DELAYS = [30, 60, 120, 300, 600, 1800, 3600, 3600, 7200]


@celery.task(bind=True, max_retries=None)
def submit_attachment(task, attachment):
    """Sends an attachment's file to the conversion service"""
    from indico_conversion.plugin import ConversionPlugin
    if ConversionPlugin.settings.get('maintenance'):
        task.retry(countdown=900)
    url = ConversionPlugin.settings.get('server_url')
    payload = {
        'attachment_id': attachment.id
    }
    data = {
        'converter': 'pdf',
        'urlresponse': url_for_plugin('conversion.callback', _external=True),
        'dirresponse': secure_serializer.dumps(payload, salt='pdf-conversion')
    }
    file = attachment.file
    name, ext = os.path.splitext(file.filename)
    # we know ext is safe since it's based on a whitelist. the name part may be fully
    # non-ascii so we sanitize that to a generic name if necessary
    filename = secure_filename(name, 'attachment') + ext
    with file.open() as fd:
        try:
            response = requests.post(url, data=data, files={'uploadedfile': (filename, fd, file.content_type)})
            response.raise_for_status()
            if 'ok' not in response.text:
                raise requests.RequestException(f'Unexpected response from server: {response.text}')
        except requests.RequestException as exc:
            attempt = task.request.retries + 1
            try:
                delay = DELAYS[task.request.retries] if not config.DEBUG else 1
            except IndexError:
                # like this we can safely bump MAX_TRIES manually if necessary
                delay = DELAYS[-1]
            try:
                task.retry(countdown=delay, max_retries=(MAX_TRIES - 1))
            except MaxRetriesExceededError:
                ConversionPlugin.logger.error('Could not submit attachment %d (attempt %d/%d); giving up [%s]',
                                              attachment.id, attempt, MAX_TRIES, exc)
                pdf_state_cache.delete(str(attachment.id))
            except Retry:
                ConversionPlugin.logger.warning('Could not submit attachment %d (attempt %d/%d); retry in %ds [%s]',
                                                attachment.id, attempt, MAX_TRIES, delay, exc)
                raise
        else:
            ConversionPlugin.logger.info('Submitted %r', attachment)


class RHConversionFinished(RH):
    """Callback to attach a converted file"""

    CSRF_ENABLED = False

    def _process(self):
        from indico_conversion.plugin import ConversionPlugin
        try:
            payload = secure_serializer.loads(request.form['directory'], salt='pdf-conversion')
        except BadData:
            ConversionPlugin.logger.exception('Received invalid payload (%s)', request.form['directory'])
            return jsonify(success=False)
        attachment = Attachment.get(payload['attachment_id'])
        if not attachment or attachment.is_deleted or attachment.folder.is_deleted:
            ConversionPlugin.logger.info('Attachment has been deleted: %s', attachment)
            return jsonify(success=True)
        elif request.form['status'] != '1':
            ConversionPlugin.logger.error('Received invalid status %s for %s', request.form['status'], attachment)
            return jsonify(success=False)
        name, ext = os.path.splitext(attachment.file.filename)
        title = get_pdf_title(attachment)
        pdf_attachment = Attachment(folder=attachment.folder, user=attachment.user, title=title,
                                    description=attachment.description, type=AttachmentType.file,
                                    protection_mode=attachment.protection_mode, acl=attachment.acl)
        data = request.files['content'].stream.read()
        pdf_attachment.file = AttachmentFile(user=attachment.file.user, filename=f'{name}.pdf',
                                             content_type='application/pdf')
        pdf_attachment.file.save(data)
        db.session.add(pdf_attachment)
        db.session.flush()
        pdf_state_cache.set(str(attachment.id), 'finished', timeout=timedelta(minutes=15))
        ConversionPlugin.logger.info('Added PDF attachment %s for %s', pdf_attachment, attachment)
        signals.attachments.attachment_created.send(pdf_attachment, user=None)
        return jsonify(success=True)


class RHConversionCheck(RH):
    """Checks if all conversions have finished"""

    def _process(self):
        ids = request.args.getlist('a')
        results = {int(id_): pdf_state_cache.get(id_) for id_ in ids}
        finished = [id_ for id_, status in results.items() if status == 'finished']
        pending = [id_ for id_, status in results.items() if status == 'pending']
        containers = {}
        if finished:
            tpl = get_template_module('attachments/_display.html')
            for attachment in Attachment.query.filter(Attachment.id.in_(finished)):
                if not attachment.folder.can_view(session.user):
                    continue
                containers[attachment.id] = tpl.render_attachments_folders(item=attachment.folder.object)
        return jsonify(finished=finished, pending=pending, containers=containers)
