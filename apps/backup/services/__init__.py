"""
Services de l'app backup : logique reutilisable hors des views.

  - utils      : helpers (hash, parsing nom de fichier, IP client)
  - scanner    : detecte les nouveaux fichiers sur disque
  - generator  : declenche un backup manuel chiffre
  - streaming  : sert un telechargement avec audit log
"""
