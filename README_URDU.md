# LocalReels v7.1 — Patch 1 (Security + PWA)

## Install kaise karna hai (2 minute)
1. Apni DB ka backup le lo (`localreels.db` ki copy kahin rakh lo).
2. Is ZIP ke andar ki sab files apne LocalReels project folder mein paste karo (Replace All).
3. `run.bat` chalao. Terminal mein ye line aayegi:
   `Phone / doosre device par edit karne ka PIN: xxxxxxxx`
   Ye PIN `.lr_token` file mein save hota hai (git mein nahi jata).
4. PC par (127.0.0.1) koi PIN nahi manga jata. Phone par pehli baar like/upload/delete karoge to PIN poochega, ek baar daalo, yaad rahega.

## Kya badla
- Leaked secret `nayi_app_secret_123` hata diya. Ab har install ka apna random PIN hai.
- Write requests (like, upload, delete, rename, download...) LAN/proxy se aayein to PIN zaroori. PC se local par nahi.
- 10 galat PIN ke baad 1 minute lockout.
- PWA: manifest ab page se link hai, `sw.js` service worker, PNG icons (192/512/maskable/iOS), install button (Android/PC) aur iPhone ke liye "Add to Home Screen" hint.
- Page hamesha network-first hai, to update foran milta hai. `/api` aur `/media` ko service worker chhoota hi nahi.
- Purana "har load par cache delete" wala code hata diya. Reset chahiye to `/?reset=1` kholo.
- `LOCALREELS_DB` env se DB path badal sakte ho (Windows path hardcode hata diya).

## GitHub safai (zaroori, repo public hai)
Project folder mein terminal kholo:

    git rm --cached localreels.db
    git rm -r --cached backups data
    git add .
    git commit -m "v7.1: PIN auth, PWA, remove private data"
    git push

Phir GitHub > Settings > Danger Zone > **Change visibility > Private**.
Purani history mein DB abhi bhi hai. Isliye repo private rakho (ya naya repo banao).
