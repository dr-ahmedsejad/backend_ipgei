"""Retire `Content-Disposition` des reponses lues en fetch.

Les gestionnaires de telechargement (Internet Download Manager en tete, mais
aussi Free Download Manager et certains antivirus) s'accrochent a l'en-tete
`Content-Disposition`. Quand le frontend recupere un document en `fetch` + blob,
IDM detourne la reponse : il redemande l'URL pour son compte, enregistre le
fichier, et TUE la requete d'origine. Chrome rapporte alors un « Failed to
fetch » double d'un message CORS trompeur — alors que le serveur a repondu 200
avec les bons en-tetes, et que le fichier est bien arrive, mais par IDM et non
par l'application. L'ecran, lui, affiche une erreur.

La parade tient a ceci : pour une reponse consommee en `fetch`, cet en-tete ne
sert a RIEN. Le navigateur ne declenche aucun telechargement depuis un blob ;
c'est le frontend qui pose le nom du fichier (`a.download`, voir
`lib/downloadBlob.ts`). L'en-tete n'a donc qu'un seul effet : reveiller IDM.

On le RETIRE pour ces seules requetes, reconnues a `Sec-Fetch-Mode` (`cors` ou
`same-origin` = fetch/XHR). Le rabattre sur `inline` ne suffisait pas : le
`filename="....pdf"` subsistait, et IDM declenche aussi sur l'extension du nom
de fichier, pas seulement sur le mot `attachment`.

Une navigation reelle — clic sur un lien, ouverture d'une URL de media — porte
`navigate` et conserve l'en-tete intact : les telechargements directs gardent
leur comportement, et IDM son role legitime. Un client qui n'envoie pas
`Sec-Fetch-*` (curl, vieux navigateur) le conserve aussi : le defaut reste le
comportement d'avant.
"""

#: Valeurs de `Sec-Fetch-Mode` qui designent un fetch/XHR, par opposition a une
#: navigation (`navigate`) ou a une sous-ressource passive (`no-cors`).
_MODES_FETCH = frozenset(('cors', 'same-origin'))


def attachment_inline_pour_fetch_middleware(get_response):
    def middleware(request):
        response = get_response(request)
        if (request.headers.get('Sec-Fetch-Mode') in _MODES_FETCH
                and 'Content-Disposition' in response.headers):
            del response.headers['Content-Disposition']
        return response
    return middleware
