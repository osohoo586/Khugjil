# Khugjil

Khan Bank income notifier: multi-tenant, security-first. A shop forwards **only** Khan Bank
transfer-notification emails from Gmail to a secret address; the service keeps only incoming
transactions to the shop's own accounts and shows them, in real time, on the cashier's screen
(PWA) and optionally in the owner's Telegram.

- Spec: [`SPEC.md`](SPEC.md) · Approved design: [`PLAN.md`](PLAN.md) · Configuration: [`docs/configuration.md`](docs/configuration.md)
- Never stored or shown: the raw email, the payer's name, the payer's account, the balance.
- No mailbox access of any kind (no IMAP/POP/Gmail API/OAuth). No LLM in the processing path.

> **Before going live** the Khan Bank values must be filled in (PLAN Q1–Q3: sender address,
> DKIM domain, subject pattern), and the `khan_v1` parser must be checked against the real email
> layout (Q6, Q8, Q9, Q11). Everything is built and tested against fake values until then.

---

## Development

Requirements: Python 3.12 + [uv](https://docs.astral.sh/uv/), Node 22+, Docker.

```sh
make services                 # throwaway Postgres (127.0.0.1:55442) + Redis (:56389)
make lint typecheck security  # ruff, mypy --strict, bandit, pip-audit
make coverage                 # pytest + per-package >= 95% coverage gate
make cashier-check            # eslint, tsc, vitest, npm audit (cashier PWA)
make e2e                      # Playwright screenshot tests (see e2e/README.md)
make agent-check              # desktop agent: capability guard, rustfmt, clippy, Rust tests
./scripts/dev-up.sh           # run it locally: https://admin.khugjil.localhost:8443 (Caddy local CA)
./scripts/dev-seed.sh         # demo data: 2 accounts, 2 cashiers (PIN 482913), today's history
./scripts/dev-seed.sh txn 15000 "Захиалга"   # one new transaction now (shows up live on the cashier)
./scripts/dev-down.sh         # stop it (--wipe also deletes the dev database)
./scripts/public-up.sh        # this Mac as the server at https://khugjil.usukh.shop (docs/mac-server.md)
```

The Windows desktop agent (Phase 2) lives in `agent/`; see [`docs/agent.md`](docs/agent.md).

Layout: `server/` (FastAPI, SMTP, worker), `cashier/` (TypeScript PWA), `shared/` (design tokens,
fonts, format test vectors), `deploy/` (Compose, Caddy, firewall, fail2ban, backups), `e2e/`.

---

## Deployment

### 1. DNS

The base domain is `khugjil.usukh.shop` (PLAN Q7), so set `KHJ_DOMAIN=khugjil.usukh.shop`. The
`usukh.shop` zone is on Cloudflare DNS; add these records there. All A records point at the
server's public IP.

| Name | Type | Value |
|---|---|---|
| `app.khugjil.usukh.shop` | A | server IP (cashier PWA) |
| `admin.khugjil.usukh.shop` | A | server IP (admin panel) |
| `in.khugjil.usukh.shop` | A | server IP (SMTP) |
| `in.khugjil.usukh.shop` | MX | `10 in.khugjil.usukh.shop.` |
| `in.khugjil.usukh.shop` | TXT (SPF) | `v=spf1 -all` (this host never sends mail) |
| `_dmarc.in.khugjil.usukh.shop` | TXT | `v=DMARC1; p=reject` |

Set every record to **DNS only** (grey cloud), not Proxied:

- Caddy obtains its own certificates with TLS-ALPN-01, which needs a direct connection on 443.
- Cloudflare's free certificate covers only one level below `usukh.shop`, not `app.khugjil.usukh.shop`.
- SMTP can't be proxied at all.
- Proxying would also let a third party terminate TLS for the admin panel and the cashier API.

Inbound TCP port 25 must be reachable (PLAN Q4). If your provider blocks it, use the Cloudflare
Email Routing fallback: [`cf-worker/README.md`](cf-worker/README.md) (`/hooks/cf`, signed requests;
set `KHJ_CF_INGEST_SECRET`).

### 2. Server

1. Install Docker, `age`, `ufw` and `fail2ban`. Apply the firewall: `SSH_FROM=<your IP> sudo deploy/ufw.sh`
   (only 25 and 443 are open; port 80 stays closed because certificates use TLS-ALPN-01).
2. Generate secrets on the server: `docker run --rm khugjil/server:local khugjil keys generate`
   (or `cd server && uv run khugjil keys generate`). Create `deploy/.env` yourself with the
   variables from `docs/configuration.md`, plus `KHJ_POSTGRES_SUPERUSER_PASSWORD` and
   `KHJ_REDIS_PASSWORD`. Set `KHJ_MIGRATION_DATABASE_URL` to the `khugjil_owner` role and
   `KHJ_DATABASE_URL` to the `khugjil_app` role. Set
   `KHJ_SMTP_TLS_CERT=/certs/caddy/certificates/acme-v02.api.letsencrypt.org-directory/in.khugjil.usukh.shop/in.khugjil.usukh.shop.crt`
   and the matching `.key`.
3. Start the database and create the roles (passwords as psql variables, never in files):
   ```sh
   cd deploy
   docker compose up -d postgres
   docker compose exec postgres psql -U postgres -v owner_pw='…' -v app_pw='…' -f /init/init-roles.sql
   ```
4. Start everything: `docker compose up -d --build` (migrations run once in the `migrate` service).
5. Create the first tenant and open the printed owner invite link:
   `docker compose exec api khugjil tenant create --name "Дэлгүүрийн нэр"`
6. Optional Telegram: create a bot with @BotFather (privacy mode **on**), set the token, secret and
   username in `deploy/.env`, then `docker compose exec api khugjil telegram set-webhook`.
7. Backups: add `AGE_RECIPIENT=age1… deploy/backup.sh` to the host's crontab (nightly). Keep the age
   private key offline.
8. fail2ban: copy `deploy/fail2ban/jail.d/*` and `deploy/fail2ban/filter.d/*` into `/etc/fail2ban/`.

Retention: transactions are deleted after `KHJ_RETENTION_DAYS` (default 90, PLAN Q5); email
outcome records after 30 days.

---

## Gmail шүүлтүүр тохируулах заавар

Энэ үйлчилгээ таны имэйлийн хайрцагт **нэвтрэхгүй**. Та зөвхөн Хаан банкны гүйлгээний мэдэгдлийг
тусгай хаяг руу дамжуулах шүүлтүүр тохируулна.

1. Админ хэсгийн **Имэйл холболт** хуудаснаас **Хүлээн авах хаяг**-ийг хуулна.
2. Gmail-ийг компьютерээр нээж, баруун дээд буланд **⚙ → Бүх тохиргоог харах** гэж орно.
3. **Дамжуулалт ба POP/IMAP** хэсэгт **Дамжуулах хаяг нэмэх** товчийг дарж хуулсан хаягаа оруулна.
4. Gmail баталгаажуулах код илгээнэ. Код **Имэйл холболт** хуудсанд 24 цагийн дотор харагдана.
   Тэр кодыг Gmail-ийн тохиргоонд оруулж **Баталгаажуулах** дарна.
5. **Шүүлтүүр ба хаасан хаяг → Шинэ шүүлтүүр үүсгэх**:
   - **Хэнээс (From):** Хаан банкны мэдэгдэл илгээдэг хаяг (Имэйл холболт хуудсанд бичсэн байгаа).
   - **Гарчиг:** орлогын мэдэгдлийн гарчиг (хуудсанд бичсэн байгаа).
6. **Шүүлтүүр үүсгэх** → **Дараах хаяг руу дамжуулах** гэснийг сонгож, хүлээн авах хаягаа сонгоно.
7. Зөвхөн банкны гүйлгээний мэдэгдлийг дамжуулна уу. Нэг удаагийн код (OTP), нууц үг агуулсан
   имэйлийг систем автоматаар хаядаг ч шүүлтүүрээ аль болох нарийн тохируулна уу.
8. **Имэйл холболт** хуудасны **Тест илгээх** товчоор кассын дэлгэц дээр «Туршилт» мэдэгдэл ирж
   байгааг шалгана. Дараагийн банкны гүйлгээ ирэхэд «Сүүлд ирсэн имэйлүүд» хүснэгтэд «Хүлээн авсан»
   гэж харагдана.

Хүлээн авах хаягаа бусдад бүү өгнө үү. Задарсан гэж үзвэл **Шинэ хаяг** товчоор солиод Gmail-ийн
дамжуулалтыг шинэ хаяг руу дахин тохируулна.

## Кассын компьютер тохируулах заавар

1. Админ хэсгийн **Төхөөрөмжүүд** хуудаснаас **Код үүсгэх** дарна.
2. Кассын компьютерийн Chrome эсвэл Edge хөтчөөр `https://app.khugjil.usukh.shop/pair` хаягийг нээж,
   12 тэмдэгттэй кодыг оруулна (утсанд бол QR кодыг уншуулна). Код 10 минутын дотор нэг удаа ажиллана.
3. Кассчин нэрээ сонгож, ПИН кодоо оруулаад **Ээлж эхлүүлэх** дарна. Энэ товчийг дарснаар дуут
   дохио идэвхжиж, хөтөч мэдэгдэл харуулах зөвшөөрөл асууна — **Зөвшөөрөх** гэж сонгоно уу.
4. Хуудсыг POS програмын хажууд **нарийн цонх** (360–480 пиксел өргөн) хэлбэрээр байрлуулбал
   хамгийн тохиромжтой. Хөтчийн цэснээс **Апп болгон суулгах** сонголтоор тусдаа цонх болгож болно.
5. Ажлын төгсгөлд **Ээлж дуусгах** дарна.

### Windows 11-ийн мэдэгдлийн тохиргоо

Windows 11 өөр програм бүтэн дэлгэцээр нээлттэй үед системийн мэдэгдлийн цонхыг нуудаг. Кассын
хуудас дотор мэдэгдэл болон дуут дохио хэвээр ажиллана, гэхдээ:

1. **Тохиргоо → Систем → Мэдэгдэл** хэсэгт **«Бүтэн дэлгэцийн горимд апп ашиглаж байхад»**
   (When using an app in full-screen mode) сонголтыг **унтраана**.
2. Мөн тэнд **Тэргүүлэх мэдэгдэл** (Priority notifications) хэсэгт **Chrome** эсвэл **Edge**-ийг нэмнэ.
3. **Анхаарлаа төвлөрүүлэх** (Focus / Do not disturb) горим асаалттай бол унтраана.
4. Кассын хуудсыг бүтэн дэлгэцээр биш, POS програмын хажууд нарийн цонхоор байлгана уу.

Энэ хязгаарлалт нь хөтчийн мэдэгдэлд хамаатай. Доорх **Хугжил касс програм** (Windows) энэ
хязгаарлалтгүй: мэдэгдлээ POS програмын дээр өөрийн цонхоор харуулдаг.

## Хугжил касс програм (Windows) суулгах заавар

Касс програм нь кассын хуудсыг тусдаа програмаар нээж, орлого орох бүрт дэлгэцийн баруун доод
буланд **бүх цонхны дээр** жижиг мэдэгдэл гаргана. POS програм бүтэн дэлгэцээр нээлттэй байсан ч
харагдана. Мэдэгдэл гарахад POS програмаас курсор, гарын товчлуур салахгүй.

1. Админ хэсгийн **Төхөөрөмжүүд** хуудсанд **Төрөл → Агент** гэж сонгоод **Код үүсгэх** дарна
   (энэ сонголт серверт касс програм идэвхжсэн үед л харагдана).
2. Кассын компьютер дээр суулгах файлыг
   `https://app.khugjil.usukh.shop/agent/Khugjil_<хувилбар>_x64-setup.exe` хаягаас (эсвэл
   админаас) авч ажиллуулна. Админы эрх шаардлагагүй.
3. Windows **«Windows protected your PC»** гэсэн цонх гаргавал **More info → Run anyway** дарна.
   Програм гарын үсгийн гэрчилгээгүй тул анх суулгахад ингэж асууна. Дараагийн шинэчлэлүүд
   манай түлхүүрээр баталгаажсан тул автоматаар суугдана.
4. Програм нээгдэхэд **«Энэ компьютерийг холбох»** хэсэгт 12 тэмдэгттэй кодыг оруулна. Код 10
   минутын дотор нэг удаа ажиллана.
5. Кассчин нэрээ сонгож, ПИН кодоороо **Ээлж эхлүүлэх** дарна. Ажлын төгсгөлд **Ээлж дуусгах**.

- Цонхыг хаавал програм хаагдахгүй, дэлгэцийн баруун доод талын **тэмдэглэгээний мөрөнд**
  (system tray) үргэлжлэн ажиллана. Тэндээс **Харах** дарж цонхыг дахин нээнэ.
- Програм Windows-д нэвтрэх үед автоматаар асна. Унтраах бол tray цэсний **«Windows эхлэхэд
  асаах»**-ыг арилгана.
- **Гарах** дарвал мэдэгдэл ирэхээ болино, тиймээс ажлын цагаар бүү гараарай.
- Холбох код, нууц түлхүүр зэрэг нь Windows-ийн **Credential Manager**-т хадгалагдана. Компьютер
  алдагдсан эсвэл солигдвол админ хэсгээс тэр төхөөрөмжийг **Цуцлах** хэрэгтэй.

### Түгээмэл асуудал

- **«Холболт тасарсан» улаан мөр гарвал:** интернэт холболтоо шалгана уу. Хуудас өөрөө дахин
  холбогдож, тасарсан хугацаанд орсон гүйлгээг татна.
- **«Дуу идэвхгүй» мөр гарвал:** тэр мөрийг нэг удаа дарна уу.
- **Мэдэгдэл огт ирэхгүй бол:** ээлж эхлүүлсэн эсэх, админ хэсгийн **Тойм** хуудсан дээрх
  «Анхаарах асуудал»-ыг шалгана уу.
