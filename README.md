# Alpha Hunter

Twitter/X'te paylaşılan memecoin kontrat adreslerini (CA) toplayan, her paylaşımı
**paylaşıldığı saniyedeki gerçek zincir verisiyle** yüzleştiren ve hesapları
istatistiksel olarak sıralayan otonom bot.

Amaç bir kazıyıcı yazmak değil; **gürültüyü eleyen matematiksel bir elek** kurmak.
Piyasada "10x yaptı" diye ekran görüntüsü paylaşan yüzlerce hesap var. Bu sistem
şunu sorar: *coin uçtuktan sonra mı tweetledin, yoksa uçmadan önce mi?*

---

## Nasıl düşünüyor

Her hesap 5 bağımsız eksende ölçülür. Tek bir eksende iyi olmak yeterli değildir.

| Eksen | Ağırlık | Ne ölçer | Kimi eler |
|---|---|---|---|
| **Güvenilirlik** | 0.32 | Wilson alt sınırı (ham isabet oranı değil) | 3 atışta 3 tutturan "şanslı" |
| **Büyüklük** | 0.24 | Kazançların **medyanı** (ortalama değil) | 1 tane 100x'i olan, 40 çöpü olan |
| **Giriş kalitesi** | 0.20 | Tweet anındaki MC + yakalanan yükseliş payı | Copycat / zirvede tweetleyen |
| **Hayatta kalma** | 0.12 | Çağırdığı coinlerin rug oranı | Paralı promo hesabı |
| **Özgünlük** | 0.12 | Aynı CA'yi kaçıncı sırada paylaştı | Echo / yankı hesabı |

Sonra iki çarpan uygulanır:

- **Spray cezası** — günde 3'ten fazla tekil CA atmaya başlayınca skor düşmeye başlar,
  25/gün'de otomatik kara liste. *Günde 30 CA atan hesap insider değil, kumarbazdır.*
- **Tutarlılık** — kazançlar tek bir haftaya sıkışmışsa cezalandırılır.

### Üç kritik metrik

**1. Run Capture — copycat dedektörü**

```
capture = log(tweet_sonrası_tepe / giriş) / log(tüm_zamanların_tepesi / taban)
```

Coin 1k → 1M gittiyse (3 kat büyüklük):

| Nerede tweetledi | Capture | Yorum |
|---|---|---|
| 2k MC | **0.90** | Gerçek erken çağrı |
| 500k MC | **0.10** | Copycat, zirveye yakın girdi |
| 1M MC | **0.00** | Tam tepe — eksi puan |

**2. Wilson alt sınırı — "az ama öz" filtresi**

Ham isabet oranı küçük örneklemde yalan söyler. Wilson %95 güven aralığının
alt sınırını alır:

| Kayıt | Ham oran | Wilson alt sınırı |
|---|---|---|
| 3/3 | %100 | **0.31** |
| 30/40 | %75 | **0.60** |

Böylece "az ama isabetli sniper" doğru şekilde ödüllendirilir, "3 atışla şanslı"
ödüllendirilmez.

**3. Sürdürülen tepe — ATH kağıt üzerindedir**

30 saniye görülen fiyattan çıkamazsın. Sistem ATH yerine **en az 15 dakika
korunan tepeyi** esas alır. Fitil pump'ları böyle elenir.

---

## Mimari

```
┌──────────────────────────────────────────────────────────────────┐
│  ADIM 2 — VERİ TOPLAMA                                           │
│  Nitter RSS (bedava) → Apify (ücretli) → X API → twscrape        │
│  İlk yanıt veren kullanılır, diğerlerine geçilmez.               │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
                  ┌─────────────────────┐
                  │  CA ÇIKARICI        │  base58 doğrulama + deny list
                  │  yüksek recall      │  pump.fun / dexscreener / gmgn /
                  │  orta precision     │  birdeye / photon / bullx / axiom
                  └──────────┬──────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  ADIM 3 — TRUTH ORACLE                                           │
│  DexScreener  → token doğrulama, ana havuz, anlık MC/likidite    │
│  GeckoTerminal→ BEDAVA tarihsel OHLCV (1m / 5m / 1h)             │
│  Birdeye      → anahtar varsa: hassas 1dk mumlar                 │
│  Solana RPC   → mint/freeze yetkisi, holder yoğunluğu            │
│  RugCheck     → üçüncü taraf risk raporu                         │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  ADIM 1 — BEYİN (PostgreSQL / SQLite)                            │
│  accounts · tokens · tweets · CALLS · price_snapshots ·          │
│  account_scores · account_clusters · alerts                      │
│                                                                  │
│  Her şey CALLS tablosunda birleşir:                              │
│  (hesap, token, tweet) + T1 fotoğrafı + sonraki fiyat hareketi   │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  SKORLAMA + TELEGRAM ALARMI  │
              │  S/A tier hesap CA paylaştı  │
              │  → anında telefonuna düşer   │
              └──────────────────────────────┘
```

**Kritik detay:** Bot ne kadar sık çalışırsa veri o kadar iyi olur. Tweet
atıldıktan 12 dakika içinde yakalanırsa, DexScreener'dan alınan **anlık fiyat
doğrudan giriş fiyatıdır** — hiçbir tarihsel API'ye ihtiyaç kalmaz ve doğruluk
en yüksek seviyededir. Sistem ayrıca kendi fiyat geçmişini biriktirir
(`price_snapshots`), yani üçüncü taraf API'ler çökse bile veri birikmeye devam eder.

---

## Hızlı başlangıç (lokal)

```bash
git clone https://github.com/OrbaySkrcl/crypto-search.git
cd crypto-search
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # düzenle (aşağıya bak)

python -m alpha_hunter initdb     # şemayı kur
python -m alpha_hunter doctor     # her şey bağlanıyor mu?
python -m alpha_hunter ingest     # tweet topla
python -m alpha_hunter enrich     # fiyatlandır
python -m alpha_hunter score      # skorla + tabloyu göster
```

### İlk gerçek test: bildiğin bir hesabı sına

```bash
python -m alpha_hunter backfill cryptohesabi --days 60
python -m alpha_hunter inspect cryptohesabi
```

`inspect` her çağrıyı tek tek gösterir: giriş MC'si, ATH katı, gerçekçi kat,
giriş kalitesi, run capture, kaçıncı sırada çağırdığı ve sonuç.

### Tek bir CA'yi incele

```bash
python -m alpha_hunter token EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm
```

Fiyat, likidite, mint/freeze yetkisi, ilk 10 cüzdan yoğunluğu ve bu CA'yi
kimlerin hangi sırayla paylaştığı.

---

## Railway'e kurulum

1. Railway'de **New Project → Deploy from GitHub repo** → bu repoyu seç.
2. **+ New → Database → PostgreSQL** ekle. `DATABASE_URL` otomatik enjekte edilir
   (kod `postgresql://` → `postgresql+psycopg://` çevrimini kendi yapar).
3. **Variables** sekmesine `.env.example`'daki değişkenleri gir. Minimum:

   ```
   CHAINS=solana
   TWEET_SOURCES=nitter,apify
   APIFY_TOKEN=...              # nitter tek başına güvenilir değil
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

4. Deploy. `railway.json` başlangıç komutunu (`python -m alpha_hunter run`)
   zaten tanımlıyor. Worker açılınca kendi zamanlayıcısıyla çalışır:
   ingest 10dk, enrich 5dk, skorlama 60dk, günlük liderlik tablosu.

Log'larda `Alpha Hunter calisiyor | zincir=solana ...` satırını görüyorsan ayakta.

### Telegram kurulumu

1. Telegram'da **@BotFather**'a `/newbot` yaz, token'ı al.
2. Kendi botuna bir mesaj gönder.
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini aç, `chat.id`'yi kopyala.
4. İkisini `.env`'e yaz, sonra test et:

```bash
python -m alpha_hunter alert --test
```

---

## Komut referansı

| Komut | Ne yapar |
|---|---|
| `initdb [--drop]` | Veritabanı şemasını kurar |
| `doctor` | Tüm bağlantıları ve anahtarları kontrol eder |
| `ingest [--lookback DK]` | Tweet toplar, CA çıkarır, çağrı kaydeder |
| `enrich [--rounds N]` | Bekleyen çağrıları fiyatlandırır |
| `score [--tier A]` | Skorları hesaplar ve liderlik tablosunu basar |
| `leaderboard [--limit N]` | Mevcut sıralamayı gösterir |
| `backfill HANDLE[,HANDLE] --days N` | Belirli hesapların geçmişini analiz eder |
| `inspect HANDLE` | Tek hesabın çağrı geçmişini döker |
| `token ADRES` | Tek bir CA'yi inceler (fiyat + güvenlik) |
| `stats` | Veritabanı özeti |
| `alert --test / --leaderboard` | Telegram testi / liderlik tablosu gönderir |
| `run` | Sürekli çalışır (Railway worker modu) |

---

## Ayar rehberi

Her şey environment değişkeni. En çok işe yarayanlar:

| Değişken | Varsayılan | Ne değişir |
|---|---|---|
| `WIN_MULTIPLE` | 3.0 | Kaç kat "başarılı" sayılır |
| `SPRAY_SOFT_CALLS_PER_DAY` | 3 | Ceza bu eşikten sonra başlar |
| `SCORE_HALFLIFE_DAYS` | 45 | Eski başarıların yarı ömrü |
| `MIN_CALLS_FOR_RATING` | 4 | Bu sayının altındaki hesap `UNRATED` kalır |
| `SUSTAINED_HIGH_MINUTES` | 15 | Tepenin "gerçek" sayılması için gereken süre |
| `ALERT_MIN_ALPHA_SCORE` | 65 | Hangi tier'dan itibaren alarm gelir |
| `SEARCH_QUERIES` | — | Taranacak sorgular (`\|` ile ayrılır) |
| `WATCHLIST_HANDLES` | — | Zaman çizelgesi taranacak hesaplar |

Skor ağırlıklarını değiştirmek istersen: `W_RELIABILITY`, `W_MAGNITUDE`,
`W_ENTRY_QUALITY`, `W_SURVIVORSHIP`, `W_ORIGINALITY`.

---

## Bilmen gereken sınırlar

Bunları açıkça yazıyorum, çünkü sistemi kullanırken karşına çıkacaklar:

1. **Nitter tek başına yetmez.** Ücretsiz nitter örnekleri sürekli düşüyor —
   `doctor` çıktısında "0/5 ayakta" görürsen normal. Ciddi kullanım için
   `APIFY_TOKEN` gerekiyor (aylık birkaç dolar). Kod ikisini de destekliyor,
   nitter düşünce otomatik Apify'a geçiyor.

2. **Ücretsiz fiyat API'si yavaş.** GeckoTerminal dakikada ~30 istek veriyor.
   Bir hesabın 60 günlük geçmişini taramak dakikalar sürer. `BIRDEYE_API_KEY`
   eklersen hem hızlanır hem 1 dakikalık mumlarla hassaslaşır.

3. **Özgünlük metriği kapsamına bağlıdır.** "İlk çağıran" derken *bizim
   gördüğümüz* ilk çağıranı kastediyoruz. Tarama kapsamın genişledikçe doğrulaşır.

4. **Hayatta kalan yanlılığı (survivorship bias).** Sadece DexScreener'a kadar
   gelebilmiş tokenları görüyoruz. Doğduğu anda ölen coinler veriye hiç girmiyor.

5. **Bu sistem korelasyon bulur, insider *kanıtlamaz*.** Yüksek skorlu bir hesap
   içeriden bilgi alıyor olabilir, güçlü bir network'ü olabilir, ya da sadece
   çok iyi olabilir. Sistem "bu hesabın geçmiş çağrıları istatistiksel olarak
   şansla açıklanamıyor" der; daha fazlasını değil.

6. **X'in kullanım şartları kazımayı yasaklar.** Kendi hesabınla `twscrape`
   kullanırsan hesabın askıya alınabilir. Apify gibi üçüncü taraf servisler
   bu riski kendi tarafında taşır.

Bu bir araştırma aracıdır, yatırım tavsiyesi değildir.

---

## Geliştirme

```bash
pip install -r requirements-dev.txt
pytest -q          # 57 test
ruff check .
```

Testler ağa çıkmaz — DexScreener ve GeckoTerminal yanıtları gerçek gövde
şekilleriyle mock'lanır. `tests/test_scoring_e2e.py` üç arketip kurar
(sniper / copycat / spammer) ve sıralamanın felsefeye uymasını doğrular.

Detaylı matematik: [`docs/ALGORITHM.md`](docs/ALGORITHM.md)
