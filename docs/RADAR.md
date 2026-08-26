# Radar — Akıllı Para Konfluans Botu

> Grafik sana olan biteni gösterir. Radar **kimin aldığını** gösterir.

Bu belge iki soruyu cevaplar:
1. Referans üründeki hangi özellikler alındı, **hangileri neden atıldı**
2. Sistem nasıl kurulur, ne kadara mal olur, doğru çalıştığı nasıl anlaşılır

---

## 1. Ayıklama: neyi aldık, neyi attık

Referans bot 18 komut ve 8 arka plan katmanı sunuyor. Bunların çoğu aynı veriyi
farklı başlıkla tekrar gösteriyor ya da **ölçülemeyen** bir çıktı üretiyor.
Ölçülemeyen çıktı kazandırmaz; yalnızca meşgul eder.

Tek elek şu soru oldu: **bu özellik yanlış çıktığında bunu görebilir miyim?**

### Alınanlar

| Özellik | Neden kaldı |
|---|---|
| **Koro / Konfluans** | Tek cüzdanın alımı gürültüdür: içeriden bilgi, tesadüf, bot, hata olabilir. Birbirinden bağımsız N cüzdan aynı pencerede aynı yöne döndüğünde tesadüf ihtimali çarpımsal düşer. Sayılabilir, tarihlenebilir, ölçülebilir. Botun tek "buraya bak" sinyali budur. |
| **Sıra dışı hacim** | Tokenin *kendi* geçmişine göre normalize edilmiş tek sayı. Tek başına alım sebebi değil; konfluansın üzerine geldiğinde anlam kazanır. Bedava, kesin. |
| **Takip listesi + eşik** | Kullanıcının kendi pozisyonu. Sade, faydalı, sıfır maliyet. |
| **Güvenlik kapısı** | Sinyal doğru olsa bile token honeypot ise sonuç sıfırdır. Kart gönderilmeden önce mint/freeze yetkisi, satış vergisi, LP, holder sayısı ve yapısal oranlar kontrol edilir. |
| **Karne (kendi sicilini tutmak)** | Ürünün en önemli parçası. Her kart +1s/+6s/+24s'te ölçülür. Bir sinyal tipi para kaybettiriyorsa kullanıcının bunu görmesi gerekir. |
| **Girilebilirlik** | $2.000 likiditede görünen 50x kâğıt üzerindedir; ~%5 kaymayla ancak ~$100 girebilirsin. Her kart bu sayıyı taşır. |

### Atılanlar

| Özellik | Neden atıldı |
|---|---|
| **`/rapor` — yapay zekâ günlük/haftalık rapor** | Doğrulanamaz. "Kim toplamış, hangi başlık öne çıkmış" cümlesi yanlış olduğunda bunu ölçemezsin; yanlış olduğu için kimse hesap sormaz. Sistemde **hiç LLM yok** — bilerek. |
| **`/hook` — olta / günün balık adayları** | "İçinde balık olabilir, olmayabilir" bir sinyal değil, bir temennidir. Sıralama kriteri açıklanamıyorsa liste rastgeledir. |
| **`/sma` — 4s/günlük/haftalık ortalama tablosu** | 6 saatlik ömrü olan memecoin'de haftalık hareketli ortalama astrolojidir. Yüksek kapitalli varlıklarda bile alım kararını tek başına taşımaz. |
| **`/stock` + tokenize hisse + SEC Form 4** | İçeriden alım beyanı gerçek veridir ama tamamen ayrı bir alan: farklı veri kaynağı, farklı zaman ölçeği, farklı risk. Sade ve sağlam kalması için kapsam dışı. |
| **`/discover` — bir coinin kendi balinaları** | Anlık holder sıralaması sicil değildir. Bir cüzdanın büyük olması iyi olduğunu göstermez. Yerine: o tokene dokunan **ölçülmüş** cüzdanlar (CA raporunda). |
| **Elle seçilmiş, adresi gizli "shark kadrosu"** | Pazarlama. Kadronun neye göre seçildiği gösterilemiyorsa iddia doğrulanamaz. Radar'da kadro **hak edilir**: her cüzdan kendi alımlarından ölçülür, sicili bozulan düşer. |
| **`/incele`, `/balinatrend`, `/kılavuz`, `/support`, `/reset`, `/alarmlarım`, `/coinekle`, 7 dil** | Arayüz fazlası. `/reset` bir botun akış tasarımı bozuk olduğunda gereken şeydir — akışı bozmamak daha iyi bir çözümdür. Susturma bir komut değil, kartın altındaki bir butondur. |
| **Abonelik / ödeme / referans katmanı** | Bu bot senin için çalışıyor, satılmıyor. |

**Sonuç: 18 komut → 7 komut.** Komut sayısı azaldıkça her komutun taşıdığı
bilgi yoğunluğu artar.

---

## 2. Nasıl düşünüyor

### Kadro elle seçilmez, hak edilir

Her cüzdan üç eksende ölçülür:

| Eksen | Ağırlık | Ne ölçer | Kimi eler |
|---|---|---|---|
| **Güvenilirlik** | 0.45 | Wilson %95 alt sınırı | 3 atışta 3 tutturan "şanslı" |
| **Büyüklük** | 0.35 | Kazançların **medyanı** | 1 tane 100x'i, 40 çöpü olan |
| **Erkenlik** | 0.20 | Medyan giriş piyasa değeri | Zirvede alan copycat |

Ham isabet oranı küçük örneklemde yalan söyler:

| Kayıt | Ham oran | Wilson alt sınırı |
|---|---|---|
| 3/3 | %100 | **0.44** |
| 8/8 | %100 | **0.68** |
| 30/40 | %75 | **0.60** |

Yani "3 atışla %100" tutturan, "40 atışta %75" tutturandan **daha düşük** puan
alır. Az ama öz sniper böyle ödüllendirilir, şanslı değil.

Üç sert eleme (skorlamaya bile girmezler):

- **Sprey** — günde 12'den fazla farklı tokene dokunan cüzdan insider değil, tarama botudur.
- **Dust** — ortalama alımı $100'ın altındaki cüzdan gürültüdür.
- **Altyapı** — izlenen tokenlerin %25'inden fazlasında görünen adres bir router/MEV botudur. *(Bu filtre ancak 50+ token görüldükten sonra devreye girer; küçük evrende yanlış eler.)*

### ATH kâğıt üzerindedir

30 saniye görülen fiyattan çıkamazsın. Hem cüzdan sicili hem karne, ATH yerine
**en az 10 dakika korunmuş tepeyi** esas alır. Fitil pump'ları böyle elenir.

### Kart kendi geçmişini taşır

Her kartın altında o sinyal tipinin son 30 günlük gerçek sicili yazar:

```
sicil (30g): 42 kart · isabet %38 · medyan tepe 1.6x · medyan 24s 1.1x
```

Bu satır kötü olduğunda da yazılır. Amaç etkilemek değil, **karar verdirmek**.

---

## 3. Sinyaller — yalnızca üç tane

| Sinyal | Koşul | Ne demek |
|---|---|---|
| 🎯 **Konfluans** | N (varsayılan 3) bağımsız akıllı cüzdan, W (varsayılan 8) saat içinde aynı tokende alım | Botun tek "buraya bak" sinyali. Gece 3'te bile gönderilir. |
| 📈 **Sıra dışı hacim** | 1s hacim, tokenin kendi 24s saatlik ortalamasının ≥6 katı | Tek başına alım sebebi değil. Konfluansın üzerine geldiğinde anlamlı. |
| 🔔 **Takip listesi** | Kendi tokeninde eşiği aşan fiyat hareketi ya da akıllı para dokunuşu | Kullanıcının kendi pozisyonu. |

Her kart şunları taşır: giriş MC · likidite · yaş · 1s/24s hacim ·
**~%5 kaymayla girilebilir tutar** · güvenlik skoru ve bayrakları · cüzdan
sicilleri · sinyal tipinin geçmiş performansı · kontrat adresi · grafik ve
gezgin bağlantısı.

Karşıt kanıt gizlenmez: aynı pencerede akıllı cüzdanlardan **satış** geldiyse
kartta yazar.

---

## 4. Mimari

```
┌──────────────────────────────────────────────────────────────────┐
│  1. KEŞİF · GeckoTerminal /new_pools + /trending_pools           │
│     Likidite tabanı altındakiler hiç kaydedilmez: girilemez.     │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  2. İŞLEM ÖRNEKLEME · GeckoTerminal /pools/{p}/trades             │
│     tx_from_address → gerçek cüzdan. Botun kalbi burası.          │
│     TEK akış hem sicili kurar hem canlı konfluansı yakalar.       │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. FİYAT · DexScreener toplu uç (30 token / istek)               │
│     Kendi fiyat geçmişimizi biriktiriyoruz → tarihsel API yok.    │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  4. CÜZDAN SİCİLİ · Wilson + medyan + erkenlik, sert elemeler      │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  5. SİNYAL + GÜVENLİK KAPISI · RugCheck / GoPlus / yapısal        │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  6. KARNE · her kart +1s/+6s/+24s ölçülür → /karne                │
└──────────────────────────────────────────────────────────────────┘
```

**Kritik tasarım kararı:** İkinci adımdaki tek bedava uç hem geçmiş sicili
kurar hem de canlı konfluansı yakalar. Ayrı bir "cüzdan izleme" altyapısına
gerek yok — akıllı cüzdan zaten bizim örneklediğimiz havuzlarda alım yapıyor.
Nansen/Birdeye gibi aylık $100+ servislerin yerini tutan şey budur.

---

## 5. Maliyet: aylık **$0**

| Katman | Servis | Ücret | Anahtar |
|---|---|---|---|
| Havuz keşfi + işlem akışı | GeckoTerminal v2 | bedava, 30 istek/dk | yok |
| Fiyat / likidite / hacim | DexScreener | bedava | yok |
| Güvenlik (Solana) | RugCheck.xyz | bedava | yok |
| Güvenlik (EVM + Solana) | GoPlus Labs | bedava | yok |
| Mesajlaşma | Telegram Bot API | bedava | bot token |
| Veritabanı | SQLite dosyası | bedava | yok |
| Barındırma | Railway / Fly.io / herhangi bir VPS | $0–5 | — |

**Hiçbir ücretli API anahtarı gerekmiyor.** Bot iki değişkenle ayağa kalkar.

Kota koruması yerleşik: aktif token tavanı (varsayılan 300), tur başına aday
tavanı (12), kaynak başına token-bucket hız sınırlayıcı, günlük örnek budama.

---

## 6. Kurulum

### 6.1 Telegram botunu oluştur

1. Telegram'da [@BotFather](https://t.me/BotFather) → `/newbot` → isim ver → **token'ı kopyala**
2. Kendi botuna `/start` yaz
3. Sohbet kimliğini öğren: [@userinfobot](https://t.me/userinfobot) → `/start` → çıkan sayı senin `TELEGRAM_CHAT_ID`'in

> Bot **yalnızca** bu sohbetten komut kabul eder. Botun kullanıcı adını bulan
> başkası ne komut verebilir ne veri görebilir.

### 6.2 Yerel çalıştırma

```bash
pip install -r requirements.txt

cp .env.example .env
# .env içine yalnızca şu ikisi yeterli:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...

python -m radar diag     # önce kaynakları test et
python -m radar run      # botu başlat
```

### 6.3 Railway (7/24 çalışsın)

1. Bu depoyu GitHub'a push et
2. Railway → **New Project** → **Deploy from GitHub repo**
3. **Variables** sekmesine `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` gir
4. **Settings → Deploy → Start Command**: `python -m radar run`
5. Kalıcılık için: **Add Volume** → mount `/data`, ardından
   `DATABASE_URL=sqlite:////data/radar.db`
   *(Postgres eklersen `DATABASE_URL` otomatik gelir, elle girme.)*

> Not: `Procfile` varsayılan olarak `alpha_hunter`'ı başlatır. Radar'ı
> çalıştırmak için start command'ı yukarıdaki gibi ez ya da
> `Procfile`'daki `radar:` satırını kullan.

---

## 7. Komutlar

| Komut | Ne yapar |
|---|---|
| **CA yapıştır** | Komut gerekmez. Tam token raporu: fiyat, likidite, girilebilirlik, güvenlik bayrakları, akıllı para durumu, "geçmişte ne demiştim" çizgisi |
| `/radar [saat]` | Son sinyaller, her birinin o günden bugüne performansıyla |
| `/liste` | Takip listen; sil / sustur / rapor butonlarıyla |
| `/cuzdan [adet]` | Sistemin kendi bulduğu akıllı cüzdanlar, tam istatistikleriyle |
| `/karne [gün]` | Botun gerçek sicili, **sinyal tipine göre ayrı ayrı** |
| `/ayar [anahtar değer]` | Üç eşik: `esik_cuzdan`, `esik_likidite`, `esik_fiyat` |
| `/durum` | Veri akıyor mu, ne kadar taze |
| `/yardim` | Komut özeti |

CLI tarafı:

```bash
python -m radar diag -v    # ham API yanıtlarını da göster
python -m radar once       # tüm işleri bir kez çalıştır
python -m radar karne 30   # sicili terminalde yazdır
python -m radar cuzdan     # kadroyu yazdır
```

---

## 8. Gerçekçi beklenti

**İlk gün sinyal gelmeyecek.** Sistem şöyle ısınır:

| Süre | Ne olur |
|---|---|
| 0–6 saat | Token keşfi, fiyat geçmişi ve işlem örnekleri birikir |
| 48 saat | İlk alımlar sonuçlanır (`wallet_eval_hours`) |
| 3–5 gün | İlk cüzdanlar `wallet_min_evaluated` eşiğini geçer, kadro oluşmaya başlar |
| 5–10 gün | Kadro 3+ cüzdana ulaşır → **ilk konfluans kartları** |
| 30 gün | `/karne` istatistiksel olarak anlamlı hale gelir |

Bu bekleme bir kusur değil; sicilin satın alınamamasının doğal sonucudur.
Hacim kartları ilk günden gelir çünkü sicil gerektirmezler.

Isınmayı hızlandırmak için `RADAR_CHAINS=solana` ile tek zincirde kal ve
`MAX_ACTIVE_TOKENS`'ı düşürme — daha çok token, daha çok işlem örneği demektir.

### Botun yapamadıkları

- **Geleceği bilmez.** Ölçülmüş geçmiş davranışı gösterir, o kadar.
- **Kadro gecikmelidir.** Bir cüzdan "akıllı" ilan edildiğinde en iyi dönemi geçmiş olabilir. `/karne` bunu yakalar.
- **Yalnızca örneklediği havuzları görür.** Likidite tabanının altındaki token akışını kaçırır — bilinçli bir karardır: orada görülen kat girilebilir değildir.
- **Konfluans nedensellik değildir.** Üç cüzdanın aynı tokende buluşması aynı Telegram grubunda olmalarından da kaynaklanabilir. Bu yüzden karne var.

---

## 9. Doğru çalıştığını nasıl anlarsın

```bash
python -m radar diag
```

Her kaynak için `OK` / `HATA` ve **ham hata metni** basar. Bir kaynak düştüğünde
bot durmaz, o turu atlar. `/durum` komutu son veri tazeliğini gösterir:
son işlem verisi 🟢 30 dakikadan yeniyse akış sağlıklıdır.

Testler ağ olmadan çalışır (gerçek API gövde şekilleriyle):

```bash
python -m pytest tests/test_radar_*.py -q
```

---

*Yatırım tavsiyesi değildir. Bu bot veri gösterir, tavsiye vermez.
Kripto varlıklar yüksek risklidir; küçük hacimli coinlerde likidite bir anda
çekilebilir. Kararın da sonucun da sahibi sensin.*
