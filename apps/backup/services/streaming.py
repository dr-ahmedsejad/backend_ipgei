"""
Streaming : sert un BackupArtifact en download HTTP, avec audit log
immuable (BackupDownloadLog).

Approche : log INSERT en fin de processing (success ou echec). Si le serveur
crash pendant le stream, on perd cette unique entree d'audit — compromis
acceptable face a la complexite d'un protocole start/end qui necessiterait
soit deux INSERT, soit assouplir les triggers d'immutabilite.
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.http import FileResponse, Http404

from ..models import BackupArtifact, BackupDownloadLog
from .scanner import refresh_artifact_status
from .utils import get_client_ip


logger = logging.getLogger(__name__)


def _log_download(
    *, user, artifact, request,
    success: bool, bytes_sent: int | None = None,
    failure_reason: str = '',
) -> None:
    """Cree une entree de log (jamais update'able grace aux triggers MySQL)."""
    try:
        BackupDownloadLog.objects.create(
            user=user,
            artifact=artifact,
            ip_address=get_client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:1000],
            success=success,
            bytes_sent=bytes_sent,
            failure_reason=failure_reason[:200],
            request_id=getattr(request, 'request_id', '') or '',
        )
    except Exception:
        # Audit fail-safe : ne JAMAIS empecher le retour HTTP du download
        # parce que le log a foire. On log juste cote serveur.
        logger.exception('Echec INSERT BackupDownloadLog (user=%s artifact=%s)',
                         user.pk, artifact.pk)


def stream_artifact_download(*, user, artifact: BackupArtifact, request) -> FileResponse:
    """
    Retourne un FileResponse pour telecharger l'artifact + log l'audit.

    Raise Http404 si le fichier n'est plus disponible sur disque.

    NB : on log AVANT le stream Django (qui se fait via WSGI hors de notre
    controle). Si le client annule en cours, le log indique success=True mais
    bytes_sent reste celui qu'on avait prevu. Acceptable pour un audit grossier.
    """
    # Verif fraicheur du disque
    if not refresh_artifact_status(artifact):
        _log_download(
            user=user, artifact=artifact, request=request,
            success=False, failure_reason='fichier indisponible sur disque',
        )
        raise Http404('Sauvegarde non disponible (rotation ou suppression).')

    path = Path(artifact.file_path)

    try:
        fh = open(path, 'rb')
    except OSError as e:
        _log_download(
            user=user, artifact=artifact, request=request,
            success=False, failure_reason=f'open failed: {e}',
        )
        raise Http404('Fichier illisible.')

    # FileResponse gere le streaming + headers Content-Disposition
    response = FileResponse(
        fh,
        as_attachment=True,
        filename=path.name,
        content_type='application/octet-stream',
    )
    response['Content-Length'] = artifact.file_size_bytes
    # Anti-cache : un backup est unique, ne doit jamais rester en cache navigateur
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'

    _log_download(
        user=user, artifact=artifact, request=request,
        success=True, bytes_sent=artifact.file_size_bytes,
    )
    return response
