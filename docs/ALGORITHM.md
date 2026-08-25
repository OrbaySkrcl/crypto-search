# Skorlama algoritması — matematiksel detay

Bu belge her metriğin **neden** o şekilde tanımlandığını açıklar.
Kod karşılığı: `alpha_hunter/scoring/metrics.py` ve `alpha_hunter/scoring/engine.py`.

---

## 0. Temel birim: Call

```
Call = (hesap, token, tweet) + T1 fotoğrafı + T1 sonrası fiyat hareketi
```

`T1` = tweet'in atıldığı an (UTC). Sistemin tamamı bu zaman damgasına dayanır,
bu yüzden veritabanındaki her `datetime` sütunu `UTCDateTime` tipiyle korunur —
SQLite tzinfo saklamadığı için bu ihmal edilirse saat karşılaştırmaları sessizce bozulur.

Bir call'ın sonucu dört durumdan biridir:

| Durum | Koşul |
|---|---|
| `WIN` | sürdürülen kat ≥ `WIN_MULTIPLE` (varsayılan 3.0) |
| `LOSS` | eşiğin altında kaldı |
| `RUG` | token'ın likiditesi çekildi |
| `INVALID` | giriş likiditesi eşiğin altında, ya da fiyat çözülemedi |

`INVALID` çağrılar skora **hiç girmez** — ne payda ne paydada. Bu önemli:
yanlış pozitif bir CA (rastgele base58 dizisi, cüzdan adresi) hesabı
cezalandırmamalı.

---

## 1. Güvenilirlik — Wilson alt sınırı

```
        p + z²/2n − z·√( (p(1−p) + z²/4n) / n )
LB  =  ─────────────────────────────────────────       z = 1.96 (%95)
                    1 + z²/n
```

Ham isabet oranı (`p = k/n`) küçük örneklemde yanıltıcıdır. Wilson skoru
örneklem büyüklüğünü cezalandırır:

| k/n | p | Wilson LB |
|---|---|---|
| 1/1 | 1.00 | 0.21 |
| 3/3 | 1.00 | 0.31 |
| 8/10 | 0.80 | 0.49 |
| 30/40 | 0.75 | 0.60 |
| 100/100 | 1.00 | 0.96 |

**Neden bu:** "Az ama öz" felsefesi ancak istatistiksel olarak
desteklenebildiğinde ödüllendirilmelidir. 3/3, 30/40'tan daha az kanıttır.

### Zaman ağırlıklı varyant

Sistem `k` ve `n` yerine **etkin sayımları** kullanır:

```
w_i  = 0.5^(yaş_gün / HALFLIFE)  ×  (0.4 + 0.6 · giriş_güveni)
n_eff = Σ w_i          k_eff = Σ (w_i · kazandı_mı)
```

İki çarpan var:
- **Zaman**: 45 gün önceki başarı yarı değer eder (`SCORE_HALFLIFE_DAYS`).
- **Giriş güveni**: T1 fiyatı gerçek 1 dakikalık mumdan mı geldi (1.0),
  yoksa tahmin mi (0.3)? Belirsiz veriden çıkan sonuç daha az ağırlık alır.

Wilson formülü kesirli sayımlarla da çalışır.

---

## 2. Büyüklük — ağırlıklı medyan

```
magnitude = clamp( log₁₀(medyan_kat) / log₁₀(MOON_MULTIPLE) , 0, 1 )
```

**Medyan, ortalama değil.** Sebep:

| Portföy | Ortalama | Medyan | Magnitude |
|---|---|---|---|
| 9×(0.2x) + 1×(100x) | 10.2x | 0.2x | **0.00** |
| 10×(4x) | 4.0x | 4.0x | **0.60** |

Ortalama kullanılsaydı, tek bir şanslı 100x'i olan çöp hesap birinci olurdu.
Medyan "tipik çağrın nasıl?" sorusunu sorar.

Log ölçek kullanılıyor çünkü 100x, 10x'in on katı değil — bir kat büyüklük
daha zordur.

---

## 3. Giriş kalitesi — KURAL 1'in matematiği

İki bileşenin karışımı: `0.45 · mc_earliness + 0.55 · run_capture`

### 3a. MC erkenciliği (mutlak seviye)

```
mc_earliness = ( log₁₀(HIGH) − log₁₀(giriş_MC) ) / ( log₁₀(HIGH) − log₁₀(LOW) )
```

`LOW = 15k`, `HIGH = 10M` (ayarlanabilir).

| Giriş MC | Değer |
|---|---|
| ≤ 15k | 1.00 |
| 50k | 0.81 |
| 300k | 0.54 |
| 2M | 0.25 |
| ≥ 10M | 0.00 |

**Neden:** 8M'den 10x yapmak 40k'dan 10x yapmaktan katbekat zordur.

### 3b. Run capture (göreli konum) — copycat dedektörü

```
run_capture = log(tepe_sonrası / giriş) / log(tüm_zamanların_tepesi / taban)
```

Bu metrik şunu sorar: **coin'in toplam log-yükselişinin yüzde kaçını bu çağrı
yakaladı?**

Coin 1k → 1M gitti (`log(1000) = 3` kat büyüklük):

| Giriş | Pay | Capture |
|---|---|---|
| 2k | log(500) = 2.70 | **0.90** |
| 50k | log(20) = 1.30 | **0.43** |
| 500k | log(2) = 0.30 | **0.10** |
| 1M | log(1) = 0 | **0.00** |

`pre_ath_mc` (tweet'ten önceki tepe) burada devreye girer: coin tweet'ten
önce zaten 1M görmüşse, `tüm_zamanların_tepesi` payda büyür ve tepede
tweetleyenin capture'ı çöker. **Copycat'ın sayısal tanımı budur.**

Market cap çözülemezse (arz tahmin edilemedi) aynı hesap saf fiyat oranlarıyla
yapılır — arz sabit olduğu için sonuç aynıdır.

---

## 4. Spray & Pray cezası — KURAL 2

```
cpd = tekil (gün, token) sayısı / aktif gün
                ⎧ 1.0                              cpd ≤ 3
spray_penalty = ⎨ clamp(3/cpd, 0.25, 1.0)          3 < cpd < 25
                ⎩ 0.25                             cpd ≥ 25
```

| Çağrı/gün | Çarpan |
|---|---|
| 2 | 1.00 |
| 5 | 0.60 |
| 10 | 0.30 |
| 25+ | 0.25 |

Ayrıca `cpd ≥ 25` **ve** ≥20 tekil token olan hesaplar otomatik kara listeye
alınır (`blacklist_spammers`), skorları sıfırlanır.

Aynı tokene aynı gün 5 tweet atmak **tek çağrı** sayılır — yoksa
"coinini savunan" hesap haksız yere cezalanırdı.

---

## 5. Özgünlük — echo cezası

```
originality = e^( −gecikme_saniye / τ )        τ = 21600 (6 saat)
```

| Gecikme | Değer |
|---|---|
| İlk çağıran | 1.00 |
| 1 saat | 0.85 |
| 6 saat | 0.37 |
| 24 saat | 0.02 |

`refresh_caller_ranks` her token için çağrıları zamana göre sıralar, ilk
hesabı 1. sıra kabul eder ve herkesin gecikmesini ona göre hesaplar.

**Bilinen sınır:** "İlk" derken *bizim gördüğümüz* ilki kastediyoruz. Bu metrik
tarama kapsamına duyarlıdır ve kapsam genişledikçe doğrulaşır.

---

## 6. Hayatta kalma

```
survivorship = 1 − (rug'lı çağrıların ağırlıklı payı)
```

Rug tespiti **fiyata değil likiditeye** bakar — fiyat sıfıra gitmeden önce
likidite çekilir:

```
rug  ⟺  son_likidite < $800
     ∨  son_likidite / zirve_likidite < 0.10
```

Ek olarak `security_score` (mint/freeze yetkisi, ilk 10 cüzdan yoğunluğu,
RugCheck raporu) alarm filtresinde kullanılır: 0.3'ün altındaki tokenlar
bildirim üretmez.

---

## 7. Tutarlılık

```
consistency = 0.5 · (kazanılan hafta / çağrı yapılan hafta) + 0.5 · clamp(kazanılan hafta / 4)
```

Kazançlar tek bir haftaya sıkışmışsa bu ya şanslı bir dönemdir ya da tek bir
pump grubuna dahil olmaktır. Zamana yayılmış başarı gerçek beceridir.

---

## 8. Bileşik skor

```
raw = 0.32·wilson + 0.24·magnitude + 0.20·entry_quality
    + 0.12·survivorship + 0.12·originality

alpha = 100 · raw · spray_penalty
              · (0.75 + 0.25·consistency)
              · (0.60 + 0.40·data_confidence)
```

Son iki çarpanın **tabanı vardır** (0.75 ve 0.60) — yani tutarsız veya az
veriye sahip bir hesap cezalanır ama sıfırlanmaz.

`data_confidence = clamp(n_değerlendirilmiş / (MIN_CALLS × 3))`

### Tier eşikleri

| Tier | Skor | Anlam |
|---|---|---|
| **S** | ≥ 80 | İstatistiksel olarak olağanüstü |
| **A** | ≥ 65 | Güçlü, takip etmeye değer |
| **B** | ≥ 50 | İyi |
| **C** | ≥ 35 | Ortalama |
| **D** | ≥ 20 | Zayıf |
| **F** | < 20 | Gürültü |
| **UNRATED** | — | `< MIN_CALLS_FOR_RATING` değerlendirilmiş çağrı |

---

## 9. Koordinasyon tespiti

Aynı tokenları, aynı dakikalarda paylaşan hesaplar bir **promo ağıdır**.
Tek tek başarılı görünebilirler ama aynı pump'ı besledikleri için bağımsız
sinyal değildirler.

```
jaccard(A, B) = |A ∩ B| / |A ∪ B|

küme  ⟺  ortak_token ≥ 3
      ∧  jaccard ≥ 0.5
      ∧  medyan_zaman_farkı ≤ 45 dakika
```

Union-find ile gruplanır, `account_clusters` tablosuna yazılır ve liderlik
tablosunda ⚠ ile işaretlenir.

---

## 10. Referans senaryo

`tests/test_scoring_e2e.py` üç arketipi aynı 6 token üzerinde kurar ve
gerçek skorları üretir:

| Hesap | Alfa | Tier | Win | Wilson | Medyan | Giriş kal. | Özgün | Spray |
|---|---|---|---|---|---|---|---|---|
| **@sniper** | **55.0** | B | %67 | 0.297 | 50.00x | 0.714 | 1.000 | 1.000 |
| @copycat | 19.3 | F | %0 | 0.000 | 2.22x | 0.229 | 0.607 | 1.000 |
| @spammer | 9.3 | F | %20 | 0.116 | 0.67x | 0.274 | 1.000 | 0.300 |

Sniper ile copycat **birebir aynı coinlere** girdi. Tek fark ne zaman
girdikleri — ve algoritma bunu 55'e 19 olarak ayırt ediyor.

Sniper'ın S değil B olması da doğrudur: yalnızca 6 değerlendirilmiş çağrısı
var, `data_confidence` düşük. Sistem az veriyle yüksek iddiada bulunmaz.
