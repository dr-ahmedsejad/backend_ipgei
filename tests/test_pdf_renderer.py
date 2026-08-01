"""
Test du renderer PDF partagé core/pdf_renderer.render_pdf_response.

pdfkit (binaire wkhtmltopdf) et le template sont mockés : on vérifie le contrat
(HttpResponse PDF + Content-Disposition, 500 si pdfkit absent) sans dépendre du
binaire ni d'un template réel.
"""
import sys
from unittest.mock import MagicMock, patch


@patch('core.pdf_utils.get_institution_context', return_value={})
@patch('core.pdf_renderer.get_template')
def test_render_pdf_response_renvoie_pdf(mock_get_template, _mock_inst):
    mock_get_template.return_value.render.return_value = '<html>x</html>'
    with patch('pdfkit.from_string', return_value=b'%PDF-1.4 fake') as mock_fs, \
         patch('pdfkit.configuration', return_value=object()):
        from core.pdf_renderer import render_pdf_response
        resp = render_pdf_response('un_template.html', {}, 'fichier.pdf')

    assert resp.status_code == 200
    assert resp['Content-Type'] == 'application/pdf'
    assert resp.content.startswith(b'%PDF')
    assert 'fichier.pdf' in resp['Content-Disposition']
    mock_fs.assert_called_once()


def test_render_pdf_response_sans_pdfkit_renvoie_500():
    # Simule l'absence de pdfkit : l'import dans la fonction lève ImportError.
    from core.pdf_renderer import render_pdf_response
    with patch.dict(sys.modules, {'pdfkit': None}):
        resp = render_pdf_response('un_template.html', {}, 'fichier.pdf')
    assert resp.status_code == 500


@patch('core.pdf_renderer.get_template')
def test_documents_render_pdf_renvoie_bytes(mock_get_template):
    """documents/_render_pdf (migré) renvoie toujours des bytes."""
    mock_get_template.return_value.render.return_value = '<html>doc</html>'
    with patch('apps.documents.services._mark_sockets_non_inheritable'), \
         patch('pdfkit.from_string', return_value=b'%PDF-doc') as mock_fs, \
         patch('pdfkit.configuration', return_value=object()):
        from apps.documents.services import _render_pdf
        out = _render_pdf('tpl.html', {})
    assert out == b'%PDF-doc'
    mock_fs.assert_called_once()


@patch('core.pdf_renderer.get_template')
def test_evaluations_render_pdf_renvoie_response(mock_get_template):
    """evaluations/views_helpers/_render_pdf (migré) renvoie un HttpResponse PDF."""
    mock_get_template.return_value.render.return_value = '<html>eval</html>'
    with patch('pdfkit.from_string', return_value=b'%PDF-eval'), \
         patch('pdfkit.configuration', return_value=object()):
        from apps.evaluations.views_helpers import _render_pdf
        resp = _render_pdf('tpl.html', {}, 'f.pdf')
    assert resp.status_code == 200
    assert resp.content == b'%PDF-eval'
    assert 'f.pdf' in resp['Content-Disposition']
