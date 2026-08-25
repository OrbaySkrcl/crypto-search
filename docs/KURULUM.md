# Adım adım kurulum ve kullanım

> Bu rehberin tarayıcıda okunan, işaretlenebilir hâli:
> [`docs/kurulum.html`](kurulum.html) — indirip çift tıklayarak açabilirsin.

Bu rehber **kodlama bilmediğini** varsayarak yazıldı. Kopyala-yapıştır yaparak
ilerleyebilirsin. Toplam süre: yaklaşık **45 dakika**.

İki yol var:

| | **Yol A — Railway** (önerilen) | **Yol B — Kendi bilgisayarın** |
|---|---|---|
| Bilgisayarın açık kalmalı mı? | Hayır | Evet, 7/24 |
| Kurulum zorluğu | Kolay, hep tarayıcıdan | Orta, terminal gerekir |
| Maliyet | Ayda ~5$ (Railway) | Ücretsiz |
| Kimin için | **Senin için bu** | Önce denemek isteyenler |

Aşağıda önce Yol A'yı anlatıyorum. Yol B en sonda.

---

# BÖLÜM 1 — Önce hesapları aç (15 dk)

Kuruluma başlamadan bunları hazırla. Hepsini bir not defterine yapıştır,
birazdan hepsini tek tek gireceksin.

## 1.1 · Telegram botu (5 dk, ücretsiz) — **zorunlu**

Alarmların telefonuna düşmesi için.

1. Telegram'da arama kutusuna **@BotFather** yaz, mavi tikli olanı aç.
2. `/newbot` yaz, gönder.
3. Bota bir isim ver (örn. `Alpha Hunter`).
4. Bir kullanıcı adı ver — **`bot` ile bitmeli** (örn. `benim_alpha_hunter_bot`).
5. BotFather sana şuna benzer bir şey verecek:

   ```
   8123456789:AAHk3xY_pQrStUvWxYz1234567890abcdefg
   ```

   **Bu senin `TELEGRAM_BOT_TOKEN`'ın.** Not defterine yapıştır.
   Kimseyle paylaşma — bu şifre gibidir.

6. Şimdi **kendi chat id'ni** bulman lazım. Yeni oluşturduğun bota git
   (kullanıcı adıyla ara), **Start**'a bas ve bir mesaj yaz — herhangi bir şey,
   "merhaba" yeter.
7. Tarayıcıda şu adresi aç (`<TOKEN>` yerine yukarıdaki tokenı yapıştır):

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   Karşına karışık bir metin çıkacak. İçinde şuna benzer bir yer ara:

   ```
   "chat":{"id":123456789,"first_name":...
   ```

   **`id` yanındaki sayı senin `TELEGRAM_CHAT_ID`'in.** Not defterine yapıştır.

   > Boş `{"ok":true,"result":[]}` görüyorsan: bota mesaj atmayı unutmuşsundur.
   > Mesaj at, sayfayı yenile.

## 1.2 · Apify (5 dk, ~5$/ay) — **şiddetle önerilir**

Twitter'dan veri çekmek için. Ücretsiz alternatifi (Nitter) var ama
sürekli düşüyor — sistemin gerçekten çalışması için buna ihtiyacın olacak.

1. [apify.com](https://apify.com) → **Sign up** (Google ile giriş yapabilirsin).
2. Sol menüden **Settings** → **API & Integrations**.
3. **Personal API token** kutusundaki uzun metni kopyala
   (`apify_api_...` ile başlar). **Bu `APIFY_TOKEN`.**

> Apify'ın ücretsiz katmanı ayda 5$ kredi veriyor. Bot 10 dakikada bir
> çalıştığında bu genelde yetmez; ayda ~5–15$ ayırmayı bekle. Bütçen yoksa
> `TWEET_SOURCES=nitter` bırak, sistem yine çalışır ama veri düzensiz gelir.

## 1.3 · Birdeye (3 dk, ücretsiz katman var) — **önerilir**

Fiyat geçmişini hassas almak için. Olmazsa sistem GeckoTerminal'i kullanır
(ücretsiz ama yavaş).

1. [bds.birdeye.so](https://bds.birdeye.so) → kaydol.
2. **API Keys** → yeni anahtar oluştur → kopyala. **Bu `BIRDEYE_API_KEY`.**

## 1.4 · Helius (3 dk, ücretsiz) — **isteğe bağlı**

Scam token tespiti için (mint yetkisi, cüzdan yoğunluğu). Olmazsa sistem
herkese açık Solana RPC'sini kullanır — yavaş ama çalışır.

1. [dashboard.helius.dev](https://dashboard.helius.dev) → kaydol.
2. Ana ekranda görünen **API Key**'i kopyala. **Bu `HELIUS_API_KEY`.**

## 1.5 · Pano şifresi (10 saniye) — **zorunlu**

Kendin uydur. Örn. `Kahve!2026_Panom`. **Bu `WEB_PASSWORD`.**

> Bunu boş bırakırsan bulduğun alfa hesapları internette herkese açık olur.

---

# BÖLÜM 2 — Railway'e kur (15 dk)

## 2.1 · Projeyi oluştur

1. [railway.app](https://railway.app) → **Login with GitHub**.
2. **New Project** → **Deploy from GitHub repo**.
3. GitHub'a erişim izni ver, sonra **`OrbaySkrcl/crypto-search`** deposunu seç.
4. Railway hemen kurmaya başlayacak. **Şimdilik hata verirse normal** —
   veritabanı ve ayarlar henüz yok.

## 2.2 · Veritabanı ekle **ve servise bağla**

1. Aynı proje ekranında **+ Create** (veya **+ New**) → **Database** →
   **Add PostgreSQL**. Kutu **Online** olana kadar bekle.

2. **BU ADIMI ATLAMA.** Railway veritabanını uygulamana otomatik bağlamaz —
   ikisini elle eşleştirmen gerekir. `crypto-search` servisine tıkla →
   **Variables** → **+ New Variable**:

   ```
   İsim  : DATABASE_URL
   Değer : ${{Postgres.DATABASE_URL}}
   ```

   Değeri olduğu gibi yaz — süslü parantezler dahil. Bu, Railway'in
   "variable reference" sözdizimi; gerçek şifreyi kendisi yerine koyar.
   (Postgres servisinin adı farklıysa `Postgres` yerine onu yaz.)

3. Kaydet. Railway yeniden kuracak.

> **Neden önemli:** bu bağlantı yoksa bot konteynerin içindeki bir dosyaya
> yazar ve Railway o dosyayı her deploy'da siler. Haftalarca topladığın veri
> tek bir "Redeploy" ile uçar. Loglarda `sqlite3.OperationalError` görürsen
> sebep budur.

1. Aynı proje ekranında **+ Create** (veya **+ New**) → **Database** →
   **Add PostgreSQL**.
2. Bitti. Railway `DATABASE_URL` değişkenini kendisi bağlar, sen bir şey yapmayacaksın.

## 2.3 · Ayarları gir

1. Soldaki **crypto-search** servisine tıkla (veritabanına değil).
2. **Variables** sekmesi → **Raw Editor** (veya **+ New Variable**).
3. Aşağıdakini olduğu gibi yapıştır ve `...` yerlerini kendi değerlerinle doldur:

```
CHAINS=solana,ethereum,base
TWEET_SOURCES=apify,nitter
APIFY_TOKEN=apify_api_BURAYA_SENIN_TOKENIN
BIRDEYE_API_KEY=BURAYA_SENIN_ANAHTARIN
HELIUS_API_KEY=BURAYA_SENIN_ANAHTARIN

TELEGRAM_BOT_TOKEN=8123456789:BURAYA_SENIN_TOKENIN
TELEGRAM_CHAT_ID=123456789
ALERTS_ENABLED=true
ALERT_MIN_ALPHA_SCORE=65

WEB_ENABLED=true
WEB_USER=admin
WEB_PASSWORD=BURAYA_KENDI_SIFREN

SEARCH_QUERIES=pump.fun/coin | dexscreener.com/solana | "CA:" solana | "contract:" solana

INGEST_INTERVAL_MINUTES=10
ENRICH_INTERVAL_MINUTES=5
SCORE_INTERVAL_MINUTES=60
LOG_LEVEL=INFO
```

4. **Deploy** / **Save** de. Railway yeniden kuracak.

> Anahtarın yoksa (örn. Birdeye almadıysan) o satırı **tamamen sil** —
> boş bırakma.

## 2.4 · Panoya adres ver

1. Aynı serviste **Settings** → aşağı in → **Networking** →
   **Generate Domain**.
2. Sana `crypto-search-production-a1b2.up.railway.app` gibi bir adres verecek.
3. O adresi tarayıcıda aç. Kullanıcı adı **`admin`**, şifre **senin
   `WEB_PASSWORD`'ün**.

Panoyu görüyorsan **kurulum bitti.** 🎯

## 2.5 · Çalıştığını doğrula

**Deployments** → en üstteki dağıtıma tıkla → **Logs**. Şu satırı ara:

```
Alpha Hunter calisiyor | zincir=solana kaynak=apify,nitter | ingest 10dk, enrich 5dk, score 60dk
```

Bu satır varsa bot ayakta.

Sonra Telegram'ı test et: pano adresinin sonuna hiçbir şey ekleme, bunun yerine
Railway'de **Deployments → ⋮ → Restart** de ve logları izle. İlk `ingest`
turunda tweet bulmaya başlayacak.

---

# BÖLÜM 3 — Ne bekleyeceksin

Bu en önemli bölüm. **Sistem ilk gün sana bir şey söylemez** ve bu bir hata değil.

| Ne zaman | Ne olur |
|---|---|
| İlk 10 dakika | Bot tweet toplamaya başlar. Panoda "çağrı" sayısı artar. |
| İlk 1–2 saat | İlk kontratlar doğrulanır, fiyatları çekilir. "Son çağrılar" dolar. |
| İlk 24 saat | Çağrılar "izleniyor" durumunda. Henüz kimse puanlanmadı. |
| **2–3 gün** | İlk skorlar çıkar. Çoğu hesap **UNRATED** veya **F** olur. Bu normal. |
| **1–2 hafta** | Tablo anlamlanır. B ve C tier hesaplar belirir. |
| **1 ay+** | A/S tier hesaplar ortaya çıkar. Telegram alarmları gelmeye başlar. |

**Neden bu kadar sürüyor?** Çünkü sistem bir hesabı puanlamadan önce onun
çağrılarının ne olduğunu görmek zorunda. Bir çağrı **7 gün** izlenir. Bir hesabın
puan alması için **en az 4 tamamlanmış çağrısı** olmalı. Yani matematiksel
olarak minimum 7 gün, pratikte 2–4 hafta.

> Bu bir kusur değil, tasarımın kendisi. "Hemen sonuç veren" bir sistem
> yalan söylüyor demektir.

## Hemen sonuç görmek istersen

Bildiğin bir hesabı geçmişe dönük tarat. Bu, Railway'de tek seferlik komut
çalıştırmayı gerektirir:

1. Railway'de servise git → sağ üstteki **⋮** → yoksa Railway CLI kur.
2. Ya da daha kolayı: **Yol B**'yi (kendi bilgisayarın) sadece bu iş için kullan.

Komut şu:

```
python -m alpha_hunter backfill hesapadi --days 60
```

Ardından `python -m alpha_hunter inspect hesapadi` ile o hesabın 60 günlük
tüm çağrılarını, giriş fiyatlarını ve sonuçlarını görürsün.

---

# BÖLÜM 4 — Panoyu okumak

## Liderlik tablosu

Her satır bir Twitter hesabı. **Alfa** sütunu 0–100 arası tek bir puan.

| Tier | Puan | Ne demek |
|---|---|---|
| **S** | 80+ | İstatistiksel olarak olağanüstü. Çok nadir. |
| **A** | 65+ | Güçlü. Takip etmeye değer. |
| **B** | 50+ | İyi. |
| **C** | 35+ | Ortalama. |
| **D** | 20+ | Zayıf. |
| **F** | 20 altı | Gürültü. |
| **UNRATED** | — | Henüz 4 tamamlanmış çağrısı yok. |

**Asıl bakman gereken üç sütun:**

- **giriş kal.** — Yüksekse coin'ler uçmadan önce tweetliyor. Düşükse
  (30'un altı) zirveye yakın tweetliyor, yani **copycat**.
- **özgünlük** — 100'e yakınsa kontratları ilk o buluyor. Düşükse başkasından
  kopyalıyor.
- **çağrı/gün** — 3'ün üstü şüpheli, 10'un üstü kumarbaz. "Günde 30 CA atan
  insider değildir."

**İşaretler:**
- ⚠ = Bu hesap başkalarıyla aynı coinleri aynı dakikalarda paylaşıyor.
  Muhtemelen bir **promo ağının** parçası. Bağımsız sinyal sayma.
- ⛔ = Spam nedeniyle kara listede.

Bir hesaba tıklarsan tüm çağrı geçmişi açılır: her coin'e kaçta girdi,
ne kadar yaptı, kaçıncı sırada paylaştı.

## Son çağrılar

Canlı akış. **gerçekçi** sütunu **ATH** sütunundan daha önemli — ATH kağıt
üzerindedir, 30 saniye görülen bir fiyattan satamazsın. "gerçekçi" en az
15 dakika korunan tepeyi gösterir.

**güvenlik** 0–100: mint yetkisi kapalı mı, ilk 10 cüzdan ne kadar tutuyor,
RugCheck ne diyor. 30'un altı tehlikeli.

## Telegram alarmları

Alfa skoru 65'in üstündeki bir hesap yeni bir kontrat paylaştığında
telefonuna şöyle bir mesaj düşer:

```
🟢 A TIER CAGRI
👤 @hesapadi · alfa 71.3
📊 win 64% (9/14) · medyan 8.20x · 1.2 cagri/gun

🪙 SYMBOL  EKpQGSJt...zcjm
⛓ solana · MC $84K · likidite $31K
🛡 guvenlik 78/100
⏱ token yasi 2.4 saat
🥇 bu CA'yi cagiran 1. hesap

🔗 tweet · dexscreener
```

Eşiği değiştirmek istersen `ALERT_MIN_ALPHA_SCORE` değişkenini düşür
(örn. `50`) — daha çok alarm gelir ama kalite düşer.

### Bota komut vermek

Bot sadece mesaj göndermiyor, komut da alıyor. Telegram'da **/** yazınca
kısayol menüsü açılır. `/start` yazarsan butonlu bir menü de gelir.

| Komut | Ne yapar |
|---|---|
| `/tara hesapadi 60` | **En çok kullanacağın komut.** Hesabı son 60 gün için tarar, puanlar ve bitince haber verir |
| `/top` | En yüksek alfa skorlu hesaplar |
| `/son` | Son 24 saatteki çağrılar |
| `/hesap hesapadi` | Bir hesabın detayı |
| `/token <CA>` | Kontratı inceler, kimlerin hangi sırada paylaştığını gösterir |
| `/isler` | Tarama işlerinin durumu |
| `/durum` | Sistem durumu |
| `/yardim` | Bütün komutlar |

> Bot yalnızca senin sohbetinden komut kabul eder. Botun adını bulan başkası
> kullanamaz — güvenlik sınırı `TELEGRAM_CHAT_ID`.

### Panodan hesap taratmak

Panoda **Hesap tara** sekmesi var: hesap adını ve kaç gün geriye bakılacağını
yazıp başlat. Tarama arka planda çalışır, sayfayı kapatabilirsin. Aynı sayfadaki
iş listesinden durumunu izlersin — bitince alfa skoru, isabet oranı ve medyan
katı orada görünür.

Ücretsiz fiyat API'si yavaş olduğu için 60 günlük bir hesap birkaç dakika sürebilir.

---

# BÖLÜM 5 — Sorun giderme

| Belirti | Sebep | Çözüm |
|---|---|---|
| **Deploy logları tamamen boş** | Build aşamasında patladı, deploy hiç başlamadı | Railway'de **Deployments → ilgili dağıtım → Build Logs** sekmesine bak (Deploy Logs değil). Gerçek hata orada. |
| **Loglarda `sqlite3.OperationalError`** | PostgreSQL bağlı değil, bot konteyner içindeki dosyaya yazıyor | Railway'de veritabanı eklentisi var mı bak. Yoksa **+ Create → Database → Add PostgreSQL**. **Veri her deploy'da siliniyor demektir — acil.** |
| **Loglarda sürekli `hicbir ornek yanit vermedi`** | Nitter örnekleri ölü, veri gelmiyor | `APIFY_TOKEN` gir ve `TWEET_SOURCES=apify,nitter` yap. Nitter tek başına çalışmıyor. |
| **Tarama "X kontrat bulundu ama … zincirinde" diyor** | Hesap senin takip etmediğin bir zincirde CA paylaşıyor | `CHAINS` değişkenine o zinciri ekle (örn. `solana,ethereum,base`) ve tekrar tarat. |
| **Apify çalışıyor mu bilmiyorum** | Panoda **Hesap tara → Bağlantı testi** düğmesi tek gerçek çağrı yapar | Token geçerli mi, aktör çalışıyor mu, kaç kayıt dönüyor — hepsini tek ekranda gösterir |
| **Tarama "Hiç tweet çekilemedi" diyor** | Sonuç satırının altında her kaynağın ne dediği yazar | `apify: kapali` → token yok veya günlük bütçe dolmuş · `apify: 0 tweet` → aktör adı/kredi sorunu · `nitter: 0 tweet` → ücretsiz örnekler kapalı (normal) |
| Panoda hep 0 çağrı | Tweet kaynağı çalışmıyor | Railway loglarında `hicbir kaynak ... veri dondurmedi` ara. `APIFY_TOKEN` doğru mu? |
| Loglarda `nitter ornegi basarisiz` | Nitter örnekleri düşmüş | Normal. `TWEET_SOURCES=apify,nitter` yaptıysan Apify devralır. |
| Telegram mesajı gelmiyor | Token/chat_id yanlış, veya henüz 65+ hesap yok | Önce `ALERT_MIN_ALPHA_SCORE=0` yapıp test et, sonra geri al. |
| Pano "Giriş gerekli" diyor | Şifre koruması aktif | Kullanıcı adı `admin`, şifre senin `WEB_PASSWORD`'ün. |
| Panoya hiç erişemiyorum | Domain oluşturulmamış | Settings → Networking → Generate Domain |
| Railway "Application failed to respond" | Bot henüz açılıyor | 1–2 dakika bekle, sonra logları kontrol et. |
| Herkes UNRATED | Yeterli veri yok | Bekle. Bu doğru davranış. |
| Çağrılar hep "geçersiz" | Fiyat verisi alınamıyor | `BIRDEYE_API_KEY` ekle. Ücretsiz kaynak yetişemiyor olabilir. |
| Railway faturası şişti | Bot çok sık çalışıyor | `INGEST_INTERVAL_MINUTES=20` yap. |

**Logları nasıl okurum?** Railway → servis → **Deployments** → en üstteki →
**Logs**. `ERROR` veya `WARNING` kelimelerini ara.

---

# BÖLÜM 6 — Ayarları kendine göre değiştirmek

Railway → **Variables** → değiştir → **Deploy**. Kod bilmene gerek yok.

| Ne istiyorsun | Hangi değişken | Ne yap |
|---|---|---|
| Daha çok alarm gelsin | `ALERT_MIN_ALPHA_SCORE` | `65` → `50` |
| Sadece çok erken çağrılara bak | `ALERT_MAX_ENTRY_MC_USD` | `3000000` → `500000` |
| "Başarılı" eşiğini yükselt | `WIN_MULTIPLE` | `3.0` → `5.0` |
| Spam filtresini sıkılaştır | `SPRAY_SOFT_CALLS_PER_DAY` | `3` → `2` |
| Daha eski verilere de bak | `SCORE_WINDOW_DAYS` | `120` → `180` |
| Başka zincirleri de tara | `CHAINS` | `solana` → `solana,ethereum,base` |
| Belirli hesapları takip et | `WATCHLIST_HANDLES` | `hesap1,hesap2,hesap3` |
| **Sadece manuel tarama yap (en ucuz)** | `INGEST_INTERVAL_MINUTES` | `10` → `1440` — otomatik keşif günde 1'e iner, bütçe manuel taramalara kalır |
| Bot daha seyrek çalışsın (ucuzlasın) | `INGEST_INTERVAL_MINUTES` | `10` → `30` |
| Apify faturasına tavan koy | `APIFY_DAILY_TWEET_BUDGET` | `4000` → `1500` |
| Daha az sorgu tara (ucuzlasın) | `SEARCH_QUERIES` | 4 sorgu yerine 1–2 tane bırak |
| Tur başına daha az tweet çek | `INGEST_MAX_TWEETS_PER_RUN` | `800` → `200` |

---

# YOL B — Kendi bilgisayarına kurmak

Sadece denemek veya `backfill` çalıştırmak istiyorsan.

## Windows

1. [python.org/downloads](https://python.org/downloads) → Python 3.11 veya üstünü indir.
   **Kurulumda "Add Python to PATH" kutusunu mutlaka işaretle.**
2. [git-scm.com/download/win](https://git-scm.com/download/win) → Git'i kur (hep İleri).
3. Başlat menüsüne `cmd` yaz, **Komut İstemi**'ni aç.
4. Sırayla yapıştır (her satırdan sonra Enter):

```
cd %USERPROFILE%\Desktop
git clone https://github.com/OrbaySkrcl/crypto-search.git
cd crypto-search
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Mac

1. Terminal'i aç (Spotlight → `Terminal`).
2. Sırayla yapıştır:

```
cd ~/Desktop
git clone https://github.com/OrbaySkrcl/crypto-search.git
cd crypto-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `git: command not found` derse: `xcode-select --install` yaz, kurulumu bekle,
> sonra tekrar dene.

## Ayarları gir

`crypto-search` klasöründe `.env.example` adlı dosyayı **kopyala** ve kopyanın
adını `.env` yap. Not defteriyle aç, Bölüm 1'de topladığın anahtarları
karşılıklarına yapıştır, kaydet.

## Çalıştır

```
python -m alpha_hunter initdb      # veritabanını kur (bir kez)
python -m alpha_hunter doctor      # her şey bağlanıyor mu?
```

`doctor` çıktısında yeşil `OK` görmen gereken yerler: veritabani, dexscreener,
geckoterminal. Telegram `AKTIF` olmalı.

Sonra:

```
python -m alpha_hunter run
```

Bu komut hem botu hem panoyu başlatır. Tarayıcıda **http://localhost:8000**
adresini aç. Durdurmak için terminalde **Ctrl+C**.

> Bilgisayar kapanınca bot durur ve veri toplamaz. Bu yüzden gerçek kullanım
> için Railway'i öneriyorum.

## Bilinen bir hesabı test et

```
python -m alpha_hunter backfill hesapadi --days 60
python -m alpha_hunter inspect hesapadi
```

Birkaç dakika sürebilir — ücretsiz fiyat API'si yavaştır.

---

# Son bir şey

Bu sistem **korelasyon** bulur, insider olduğunu **kanıtlamaz**. Yüksek skorlu
bir hesap içeriden bilgi alıyor olabilir, güçlü bir network'ü olabilir, ya da
sadece çok iyi olabilir. Sistemin söylediği şudur:

> "Bu hesabın geçmiş çağrıları rastgele şansla açıklanamıyor."

Daha fazlası değil. Bir hesabın S tier olması, bir sonraki çağrısının
tutacağı anlamına gelmez. Bu bir araştırma aracıdır, yatırım tavsiyesi değildir.

Diğer bilmen gereken sınırlar için ana [README](../README.md)'nin sonundaki
"Bilmen gereken sınırlar" bölümüne bak.
