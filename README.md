# Song Studio Local

Une interface locale simple devant l’API ACE-Step existante. Elle conserve les chansons, lance des lots de variations en file (une génération à la fois), permet de rechercher, écouter, renommer, marquer en favori et exporter en WAV/MP3.

## Installation

Sur le PC Windows qui fait tourner ACE-Step, avec Python 3.11+ et FFmpeg dans le `PATH` :

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
$env:ACESTEP_API_URL = "http://192.168.40.55:8001"
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Ouvrir `http://192.168.40.55:8765` sur le Mac. Si nécessaire, autoriser le port TCP 8765 sur le profil privé du pare-feu Windows. L’API ACE-Step reste à son adresse actuelle et n’est pas modifiée. L’interface parle uniquement à ce serveur Song Studio, qui relaie les tâches à ACE-Step. Pour un premier essai sur le Mac, la même commande `uvicorn` fonctionne si le Mac peut joindre `192.168.40.55:8001` ; les fichiers créés seront alors stockés sur le Mac.

Options d’environnement : `ACESTEP_API_URL`, `SONG_DATA_DIR` (défaut `./data`), `SONG_MODEL` (défaut `acestep-v15-xl-sft`) et `SONG_LM_MODEL` (défaut `acestep-5Hz-lm-1.7B`). `ACESTEP_MAINTENANCE_EVERY` fixe le nombre de morceaux réussis entre deux réinitialisations mémoire d’ACE-Step (10 par défaut, 0 pour désactiver).

Au démarrage, Song Studio charge automatiquement le fichier `.env` situé à la racine du projet. Copier `.env.example` pour créer une nouvelle configuration. Une variable déjà exportée dans le Terminal reste prioritaire. `SONG_ENV_FILE` permet exceptionnellement de charger un autre fichier.

Après chaque groupe de 10 morceaux réussis, Song Studio attend qu’aucune génération ne soit en cours, appelle la réinitialisation mémoire officielle d’ACE-Step, recharge le modèle DiT et le LM configurés, vérifie la santé du service, puis reprend la file. Le compteur est conservé en base après un redémarrage de Song Studio. Si la maintenance échoue, aucun nouveau morceau n’est soumis et elle est retentée, afin d’éviter de continuer à remplir la mémoire.

## Génération des paroles

Le mode **Paroles automatiques** utilise directement le LM 5Hz d’ACE-Step. Chaque morceau reçoit un brief distinct, dérivé de sa position et de sa seed, qui impose la langue choisie, une structure complète, une progression narrative, un type de refrain et l’interdiction du mélange de langues. `vocal_language` reste fixé par Song Studio et la détection automatique de langue est désactivée. Ces contraintes améliorent la régularité sans garantir qu’un modèle génératif ne fera jamais d’erreur : les paroles doivent être relues avant publication.

Chaque tâche suit les états persistants `draft`, `preparing`, `ready`, `submitting`, `generating`, `post_processing`, puis `completed`. Le prompt final, les paroles, leur validation, le negative prompt et le payload ACE-Step complet sont enregistrés dans SQLite avant l’appel à l’API. Après un redémarrage, les tâches préparées reprennent sans régénérer leurs paroles. Une tâche en échec n’empêche pas les suivantes du lot de continuer.

## Notifications de fin de lot

Pour envoyer un webhook JSON à la fin de chaque lot, définir `SONG_WEBHOOK_URL` avant de démarrer l’application. Exemple PowerShell :

```powershell
$env:SONG_WEBHOOK_URL = "https://votre-service.example/webhook"
```

Le serveur envoie un `POST` avec `event: "batch.finished"`, `batch_id`, `description`, `total`, `completed`, `failed` et `canceled`. Il ajoute les en-têtes `X-Song-Event` et `Idempotency-Key`. Un échec est retenté avec délai croissant ; comme pour tout webhook, le destinataire doit accepter un éventuel doublon (même clé d’idempotence). Les lots créés avant la configuration du webhook ne sont pas envoyés rétroactivement.

Pour recevoir une vraie notification push sur téléphone, installer l’app ntfy et définir l’URL d’un topic (public ou auto-hébergé) :

```powershell
$env:SONG_NTFY_URL = "https://ntfy.sh/un-topic-prive-difficile-a-deviner"
```

Un topic public ntfy n’est pas confidentiel ; utiliser une instance privée/authentifiée ou une URL adaptée pour des données sensibles. Le bouton de notification dans l’interface active aussi les notifications du navigateur lorsque celui-ci les autorise. Sur l’URL LAN en `http://192.168…`, les navigateurs les bloquent généralement car une origine HTTPS (ou `localhost`) est requise ; ntfy/webhook fonctionne même lorsque la page est fermée, tant que le serveur Song Studio reste actif.

## Utilisation

Écrire une description de la chanson, choisir explicitement **Instrumental**, **Paroles automatiques** ou **Mes paroles**, puis le nombre de variations. Instrumental est le choix par défaut et envoie `[Instrumental]` à ACE-Step. « Paroles automatiques » demande au LM interne d’ACE-Step des paroles propres à chaque morceau. La langue choisie est enregistrée et imposée via `vocal_language`. Avec « Mes paroles », utiliser des balises comme `[Verse 1]`, `[Chorus]` et `[Bridge]`. Le texte « 200 BPM » dans la description remplit le paramètre BPM automatiquement. Les tâches sont enregistrées en SQLite et restent dans la file après redémarrage. Pause n’interrompt pas la chanson déjà en cours ; elle empêche seulement le démarrage de la suivante.

### Format long instrumental

Activer **Format long instrumental** dans Réglages pour produire un mix de 30 minutes, 1 heure ou 2 heures. Song Studio crée automatiquement des segments d’environ cinq minutes, leur applique une évolution stable, progressive ou énergique, puis les assemble avec des fondus croisés adaptés au BPM. La durée de chaque segment compense les chevauchements afin que le master final corresponde à la durée demandée. Les segments utilisent la file audio normale mais restent cachés de la bibliothèque ; le projet long affiche sa progression séparément et peut être écouté ou exporté en MP3/FLAC RouteNote une fois assemblé. Un projet interrompu reste en base et ses seuls segments en échec peuvent être relancés.

Dans Réglages, **Exclusions** correspond à `lm_negative_prompt`. Si le champ reste vide, Song Studio construit automatiquement une liste adaptée : suppression des voix en mode instrumental, ou exclusion des mauvaises langues et des défauts audio en mode vocal. Un texte saisi remplace entièrement cette valeur automatique. Un negative prompt oriente le modèle mais ne garantit pas à lui seul l’absence absolue d’un élément.

### Pochettes locales

L’option **Pochette automatique** crée une image distincte pour chaque morceau terminé. La file image ne démarre que lorsqu’il ne reste plus aucune génération audio en cours ou en attente, afin de ne pas mettre ACE-Step et ComfyUI en concurrence sur le GPU. L’image source est conservée en PNG 1024×1024 ; le téléchargement « Pochette » produit un JPEG 3000×3000.

Installer ComfyUI sur le PC Windows, y placer un checkpoint SDXL, démarrer son API sur le réseau local, puis configurer avant Song Studio :

```powershell
$env:COMFYUI_API_URL = "http://192.168.40.55:8188"
$env:COMFYUI_UNET = "z_image_turbo_bf16.safetensors"
$env:COMFYUI_CLIP = "qwen_3_4b.safetensors"
$env:COMFYUI_VAE = "ae.safetensors"
```

Le workflow intégré reproduit `image_z_image_turbo` : Z-Image Turbo BF16, Qwen 3 4B, VAE `ae`, 1024×1024, 8 étapes, CFG 1, `res_multistep`, scheduler `simple` et shift 3. Il utilise uniquement les nœuds standards visibles sur cette installation. Si ComfyUI est absent ou si un modèle est mal nommé, la chanson reste disponible et seule la pochette passe en échec ; le bouton « Créer pochette » permet de la relancer après correction.

Les WAV sources, masters normalisés et MP3 320 kb/s sont conservés dans `data/audio/`. Pour chaque nouvelle génération, Song Studio conserve le rendu ACE-Step sous `*.source.wav`, puis crée automatiquement un master stéréo PCM 24 bits / 48 kHz. Le mastering utilise deux passes de mesure, vise -14 LUFS et -1 dBTP, coupe le subgrave sous 28 Hz et réduit doucement la zone 95 Hz afin de limiter les basses saturées sans écraser la dynamique. Le lecteur utilise directement ce master. Conserver une sauvegarde du dossier `data` : l’API ACE-Step seule peut supprimer ses fichiers temporaires.

Le preset XL-SFT de qualité utilise 64 étapes, une guidance de 6.5, `shift=1.0`, le sampler Heun, une fenêtre CFG 0.05–0.9 et la correction DCW. Le prompt de production impose un grave propre et séparé ; le negative prompt exclut explicitement clipping, basse distordue, subgrave excessif, pompage et mix écrasé. En mode automatique, le LM interne prépare les paroles avec décodage contraint ; en mode « Mes paroles », les réécritures correspondantes restent désactivées.

La vue du studio ne charge que les 10 derniers morceaux par défaut, avec un choix de 20. Le lien **Tous les morceaux** ouvre une page dédiée paginée par 20 éléments ; recherche et favoris sont filtrés directement dans SQLite avant pagination.

Dans **Tous les morceaux**, chaque élément terminé peut être retiré individuellement ou par sélection multiple. « Tout sélectionner » agit sur la page courante. La suppression est logique et récupérable : le morceau disparaît de la bibliothèque, mais sa ligne SQLite et ses fichiers restent conservés. Les chansons et pochettes en cours ou en attente sont protégées contre la suppression.

La même sélection permet de télécharger plusieurs morceaux dans une archive ZIP prête pour RouteNote : MP3 stéréo 320 kb/s / 44,1 kHz, FLAC stéréo 16 bits / 44,1 kHz, ou les deux formats. Les exports ne contiennent ni tags ni pochette embarquée et utilisent des noms ASCII courts sans caractères spéciaux. Le master WAV 24 bits / 48 kHz reste conservé en interne. Les conversions déjà présentes sont réutilisées et l’archive temporaire est supprimée après son envoi.

Les vérifications sont volontairement modérées : ACE-Step est interrogé toutes les 2 minutes pendant une génération et son état de santé est mis en cache 60 secondes côté serveur. L’interface locale se rafraîchit toutes les 15 secondes uniquement lorsqu’une chanson ou une pochette est en cours ou en attente. Lorsque toutes les files sont vides, le rafraîchissement automatique et les contrôles de santé d’ACE-Step s’arrêtent complètement ; une action utilisateur ou le retour sur l’onglet réveille l’interface. Un onglet actif en arrière-plan ne vérifie pas plus souvent qu’une fois par minute. La notification de fin peut donc arriver jusqu’à environ 2 minutes après la fin réelle du calcul.

Après chaque réponse de `/query_result`, le dernier état connu de la chanson (étape, message éventuel et heure du contrôle) est enregistré en SQLite et affiché dans la bibliothèque. Cela ne crée aucun appel supplémentaire vers ACE-Step et ne prétend pas mesurer une progression entre deux contrôles.

Lors de la soumission à ACE-Step, une indisponibilité réseau transitoire est retentée automatiquement 6 fois avec un délai progressif de 5 secondes (`ACESTEP_SUBMIT_RETRIES` et `ACESTEP_SUBMIT_RETRY_SECONDS`). La tâche n’est marquée en échec qu’après épuisement de ces tentatives.

Les valeurs de BPM et de durée sont des demandes au modèle, pas une garantie mesurée sur le rendu. Écouter et vérifier chaque morceau avant publication, notamment les paroles, la qualité et les conditions de licence applicables à la version d’ACE-Step utilisée.
