# Dijital İkiz Planı (v0.1, 3 Kasım 2026)

Sahibi: Zehra. Çalıştığı yer: bulut (`cloud/`), `service.py` ile aynı servis.
Sensörlere ve TinyML'e bağımlı değil: staj kaydı ve okul serası kaydıyla
(replay) baştan sona geliştirilip test edilebilir.

## 1. Ne, ne değil

- **Ne:** Her saksının bulutta yaşayan fizik modeli kopyası. Her okumayla
  kendini günceller, gerçeğe ne kadar yakın olduğunu ölçer ve "ya şöyle
  olsaydı?" sorularını cevaplar.
- **Ne değil:** 3B görselleştirme veya bitkinin tam büyüme modeli değil.
  Yalnızca **kök bölgesindeki suyun** ikizi.
- **Temel:** stajdaki "driven drying" modeli. Her 5 dakikada nem
  `Δ = a·VPD + c` kadar düşer; `a`, `c` son 24 saatten en küçük kareler ile
  fit edilir (baseline ile aynı, `docs/baseline_port_spec.md`).

## 2. Dört parça

| Parça | Ne yapar | Örnek çıktı |
|---|---|---|
| A. Senkron | Her okumada `a`, `c`'yi günceller; sulama olaylarını yakalar (nemde ani yükseliş) | "Son sulama 14:20, a = …, c = …" |
| B. Tahmin | Önümüzdeki 3–24 saat için nem eğrisi | "Eşiğe 2 sa 40 dk sonra düşer" |
| C. What-if | Modeli iki senaryoyla ileri çalıştırır | "Sulamazsan 2 sa 40 dk"; "Şimdi sularsan yarın 15:00'e kadar güvende" |
| D. Anomali | Ölçüm ile ikizin bir adım önceki tahmini arasındaki farkı (residual) izler | "Nem 12 dk'da 12 puan düştü, toprak bunu yapamaz → prob oynamış olabilir" |

Anomali türleri (D):

| Tür | Olası sebep | İlk kural |
|---|---|---|
| Ani düşüş / sıçrama | Prob yerinden oynadı, temassızlık | \|residual\| fiziksel sınırın üstünde |
| Donmuş değer | Sensör aynı değeri tekrarlıyor | N örnek boyunca değişim yok |
| Beklenmeyen yükseliş | Kayıtsız sulama, sızıntı | Sulama olayı ama kayıt yok |
| Yavaş kayma | Bitki büyümesi, FC kayması | `a`, `c` günler içinde eğilimli değişiyor |

Gerçek örnek: staj kaydında 9 Eylül'de prob yerinden oynadı, nem 12 dakikada
12,8 puan düştü (staj raporu, Table 2). İkiz bunu otomatik yakalamalı.

## 3. Sistemdeki yeri

```
reading → service.py ─┬─ baseline / Chronos → sagriot/<node>/cloud_decision
                      └─ twin.py (A, B, C, D) → sagriot/<node>/twin
                                                    ↓
                                       Telegram uyarısı ("sulamazsan 2 sa 40 dk")
```

- `twin.py`, `service.py`'deki düğüm başına 24 saatlik tamponu paylaşır.
- Çıktı yeni konu: `sagriot/<node>/twin` (alanlar `docs/message_contract.md`'ye
  eklenecek: `a`, `c`, `last_irrigation`, `t_cross_min`, `whatif`,
  `anomaly`, `residual`).
- Telegram botu bu çıktıyı kullanır.

## 4. Doğrulama (ölçüme dayalı)

- **Tahmin:** staj ve okul serası kayıtlarında ufka göre hata (1, 3, 6 sa).
- **Anomali:**
  1. 9 Eylül gerçek olayını yakalıyor mu, kaç dakika sonra?
  2. Kayda yapay anomali eklenir (donmuş değer, sıçrama, kayma); tespit oranı
     ve gecikme ölçülür.
  3. Normal günlerde yanlış alarm oranı.
- **What-if:** "Sulamazsam?" tahmini, sulanmayan dönemde gerçekleşen eğriyle;
  "Şimdi sularsam?" tahmini, bir sonraki gerçek sulamadan sonraki eğriyle
  karşılaştırılır. (Geçmişe dönük "sulasaydık" türü senaryolar doğrudan
  doğrulanamaz; kapsam dışı.)

## 4a. Saksının durumu ve test senaryoları

Saksının üç durumu var (Telegram ve grafikte aynı semboller):

| Durum | Ne zaman | İkizin parçası |
|---|---|---|
| 😊 **Mutlu** | Nem iyi; eşiğe 3 saatten fazla var. Sulama yakalanınca da bu duruma döner | A. Senkron, B. Tahmin |
| 😟 **Susamış** | Eşiğe 3 saatten az kaldı (`warn`) ya da eşiğin altında (`now`). Mesajda kalan süre veya "şimdi sula" | B. Tahmin |
| ⚠️ **Arıza** | Ölçüm fiziğin izin vermediği bir şey yapıyor (ani düşüş, donmuş değer) | D. Anomali |

Arıza durumu diğerlerinin önüne geçer: sensöre güvenilmiyorsa "sula" denmez.

**Senaryo 1: Kuruma döngüsü** (staj kaydından bir sulama döngüsü, replay)
- Beklenen sıra: 😊 → 😟 → (sulama) → 😊
- Ölçülen: 😟 eşik geçilmeden kaç dakika önce geldi (staj: zamanlama hatası
  16,8 dk); sulama gerçekleştikten kaç dakika sonra 😊'a dönüldü.

**Senaryo 2: Sensör arızası** (staj kaydı, 9 Eylül prob kayması: 12 dk'da 12,8 puan düşüş)
- Beklenen: 😊 → ⚠️ (**😟 değil**)
- Basit eşik kuralı burada "sula" derdi; toprak bu kadar hızlı kuruyamaz.
  İkiz gerçek susuzluğu sensör arızasından ayırır.
- Ölçülen: arıza kaç dakikada yakalandı; o sırada yanlış "sula" uyarısı
  üretildi mi.

**Çiftçinin deneyebileceği iki what-if** (Telegram'da durum mesajının altında butonlar)

| Buton | Ne hesaplar | Cevap örneği | Doğrulama |
|---|---|---|---|
| 🔮 Sulamazsam? | Sulama olmadan, bugünkü VPD sürerse 24 saat ileri | "Bugün 16:40'ta susamış, yarın 07:10'da kritik" | Sulanmayan dönemde tahmin edilen saat ile gerçekleşen saat |
| 💧 Şimdi sularsam? | Nemi FC'ye çek, sonra ileri çalıştır | "Yarın 15:00'e kadar yeter" | Bir sonraki gerçek sulamadan sonraki eğri |

- İkisi birbirini tamamlıyor: sulamazsam / sularsam yolları yan yana.
- "Şimdi sularsam" için sulama sonrası nemin FC'ye çıktığı varsayılır; okul
  serasında FC ölçülmediği için sulama sonrası platodan tahmin edilir
  (Açık noktalar).
- Kapsam dışı: VPD artışı ("yarın sıcak olursa"), "X saatine kadar idare eder
  mi", sulama miktarı.

## 4b. İkizden düğüme geri bildirim: gönderim sıklığı (2 Aralık sonrası)

**Aşama:** 2 Aralık'ta ikiz tek yönlü (fiziksel → dijital) gösterilir; bu
bölüm sonraki aşamada eklenir ve ikizi çift yönlü yapar.

Amaç: veri iki yönde de otomatik aksın (tanım gereği "digital twin"; yalnız
fiziksel → dijital akış "digital shadow" olurdu) ve ikiz düğümün enerjisini
bitkinin durumuna göre yönetsin.

**Ölçüm sıklığı değişmez, gönderim sıklığı değişir.** Model 5 dakikalık
örneklere (24 sa = 288) dayanıyor; düğümde enerjinin büyük kısmını Wi-Fi
harcıyor.

| Durum | Ölçüm + edge kararı | Wi-Fi / gönderim |
|---|---|---|
| 😊 Mutlu | 5 dk'da bir | 15 dk'da bir, biriken 3 ölçüm toplu gönderilir |
| 😟 Susamış | 5 dk'da bir | her ölçüm hemen |
| ⚠️ Arıza | 5 dk'da bir | her ölçüm hemen |

- **Mekanizma:** ikiz durum değişince `sagriot/<node>/config` konusuna
  **retained** mesaj yayınlar, ör. `{"v":1,"send_every":3}` /
  `{"v":1,"send_every":1}`. Düğüm uyanıp bağlandığında son config'i alır,
  sonraki döngülerde buna göre biriktirir.
- **Güvenlik kuralları:**
  - Config yoksa veya okunamadıysa varsayılan `send_every = 1` (her ölçüm
    gönderilir).
  - Düğüm kendi edge kararında `warn` veya `now` görürse ikizi beklemeden
    hemen gönderir; uyarı gecikmez.
- **Düğüm tarafı gereksinimi:** ölçümleri RTC belleğinde biriktirme, `seq`'i
  RTC'de tutma, toplu gönderim (zaten planlı: deep sleep işi, Rüya ile).
- **Ölçüm (rapor için):** Rüya'nın akım tablosuyla günlük enerji: sabit
  "her ölçüm gönder" vs. ikizin yönettiği gönderim; ayrıca günlük Wi-Fi
  açılma sayısı.
- **Sıra:** önce ikizin config yayınlaması, sonra düğümün buna uyması
  (Rüya'nın deep sleep + RTC tamponu işiyle birlikte).

## 5. Takvim

| Ne zaman | İş | Süre |
|---|---|---|
| 20–23 Kas | `service.py` (baseline + Chronos), ikizin temeli | ~10 sa |
| 24–25 Kas | A + B + D, staj kaydında doğrulama | ~6 sa |
| 26 Kas | C: "Sulamazsam?" + "Şimdi sularsam?" | ~2 sa |
| 26–27 Kas | Local web paneli (gateway'de): durum kartı, nem grafiği, what-if butonları | ~4–6 sa |
| 27–29 Kas | Entegrasyon; Telegram butonları | |
| 2 Ara sonrası | 4b: çift yönlü ikiz (config yayını + düğümde toplu gönderim), enerji ölçümü | |

**Minimum sürüm (zaman sıkışırsa):** A + B + D'de "ani düşüş" kuralı + tek
grafik. 1–2 gün; "ikiz çalışıyor ve stajdaki gerçek arızayı yakalıyor".

## 6. YZ bağlantısı

İlk sürümde anomali kuralları basit eşiklerle. Zaman kalırsa residual'lar
üzerinde öğrenen bir model (ör. Isolation Forest) eklenir. 2 Aralık için şart
değil.

## Ön sonuç: okul serası kaydı (3 Kasım, ilk deneme)

Kayıt: `lab_log.csv`, sürekli kısım 6 Ekim 17:00 – 26 Ekim (≈20 gün,
5 dk'ya ortalanmış 5.766 örnek; 1–6 Ekim arası kurulum boşlukları dışarıda).

- **Sulama olayları:** 7 (9, 12, 14, 19, 21, 23, 26 Ekim), nemde ani
  yükselişle kolayca yakalanıyor (A için iyi haber).
- **FC tahmini:** sulamadan 6–7 sa sonraki plato ≈ 22–24 %VWC → **FC ≈ 23,5**
  (staj saksısında 64,9; toprak/saksı farklı). 70 % FC eşiği ≈ 16,4 %VWC.
  Bu eşiğin altına inilen tek dönem: 10–12 Ekim (en düşük 14,7). Karar
  düzeyinde değerlendirme için olay sayısı az (1) → sınırlama.
- **Kuruma hızı:** yavaş, ≈1–1,5 puan/gün; prob çözünürlüğü 0,1 puan
  (5 dk'lık adımların çoğu 0).
- **Tahmin (B), 3 sa ufuk, 433 pencere, sulama içeren ufuklar hariç:**
  model MAE 0,095 puan vs persistence 0,144 → **persistence'a göre %34 daha
  iyi**; 3 sa sonundaki hata 0,17 vs 0,27 puan. VPD için persistence ya da
  24 sa önceki değer kullanmak sonucu değiştirmiyor (%34 / %35).
- **Anomali (D) için önemli bulgu:** en büyük negatif residual'lar
  (−1,8, −1,6 puan) **sulamadan hemen sonraki drenajda** çıkıyor; fiziksel bir
  arıza değil. Kural: sulama yakalandıktan sonraki ~1 sa boyunca "ani düşüş"
  anomalisi bastırılmalı, yoksa her sulamada yanlış ⚠️ olur. Normal dönemde
  |residual| %99'luk dilimi 0,085 puan → arıza eşiği bunun çok üstünde
  seçilebilir (9 Eylül olayı 12 dk'da 12,8 puan).

Betik ve ara dosyalar şimdilik repoda değil (Claude scratchpad); `service.py`
yazılırken `experiments/` altına taşınacak.

## Açık noktalar

- Okul serası FC'si ölçülmedi; plato yöntemiyle ≈23,5 tahmin edildi (yukarıda),
  doğrulanmalı.
- `twin` mesaj alanları ve güncelleme sıklığı (her okuma mı, saatlik mi).
- Görselleştirme (karar, 3 Kasım): çiftçi için Telegram; ekip, hoca ve jüri
  için **local web paneli** (gateway'de, Flask/FastAPI + Chart.js; veri gateway
  SQLite + `sagriot/<node>/twin`). İnternete açık değil, şifre gerekmez. İkiz
  hazır olmadan da okumaları ve edge/cloud kararlarını gösterebilir. Konsept:
  "SAgrIoT Dijital İkiz" tasarımı (PDF hocaya gönderildi). Hocanın görüşü
  soruldu.
