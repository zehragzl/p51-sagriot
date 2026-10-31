# Entegrasyon Planı (v0.1, 26 Ekim 2026)

Bu doküman üç parçanın (uçtan uca sistem, donanım, ML) nasıl birleşeceğini ve
her parçadan ne beklendiğini anlatır. Mesajların ayrıntısı:
`docs/message_contract.md`.

## 1. Sistem

```
ESP32-S3 düğüm  --MQTT-->  Gateway (Pi)  -->  Bulut
 firmware/                  gateway/          cloud/
```

**Kurulum:** Bulut servisi bu hafta gerçek bir bulut sunucusuna (VM, Docker)
taşınıyor. Gateway şimdilik Mac (Mosquitto + `gateway/logger.py`); Mac'teki
Mosquitto, buluttaki Mosquitto'ya köprü (bridge) ile bağlanır:

```
ESP32 -> Mac Mosquitto (gateway) == bridge ==> Bulut Mosquitto <-> cloud/service.py
```

Gateway ileride gerçek bir Pi'ye taşınacak; kod değişmez, sadece adresler değişir.
Buluttaki broker internete açık olduğu için kullanıcı adı/şifre (ve mümkünse TLS)
zorunlu; anonim bağlantı yok.

**İki çalışma modu** (karşılaştırmanın kendisi):

| Mod | Düğüm ne yapar | Bulut ne yapar |
|---|---|---|
| edge | Kararı kendisi verir (baseline + TinyML), `decision` gönderir | Sadece kaydeder |
| cloud | Ölçümü gönderir (`reading`) | Kararı verir (baseline, Chronos, float ML), `cloud_decision` döndürür |

Her iki modda da karar kuralı aynı: 3 saatlik nem tahmini, `soil_fc < 70`
(katı) olan ilk adım uyarıyı verir. Böylece modeller adil karşılaştırılır.

## 2. Düğümün döngüsü (Zehra, `firmware/main/main.c`)

```
uyan -> sensörleri oku -> 24 saatlik tampona ekle -> karar ver
     -> Wi-Fi + MQTT ile gönder -> uyu (5 dk)
```

- Ana döngü Zehra'da. Sensör okuma ve uyku fonksiyonları Rüya'dan,
  TinyML çağrısı Derya'dan gelir; döngü bunları çağırır.
- Bugün sensör yerine kayıtlı veri oynatılıyor (`components/replay`).
  Sürücü gelince yalnızca `read_sample()` değişir.

## 3. Donanım (Rüya) — `firmware/components/sensors/`

### Beklenen arayüz

```c
typedef struct {
    float vwc;      // % (prob, register 0x0001 / 100)
    float soil_t;   // °C (prob, register 0x0000 / 100, işaretli)
    float ec;       // µS/cm (prob, register 0x0002)
    float air_t;    // °C (SHT31)
    float rh;       // % (SHT31)
    uint32_t err;   // 0 = hepsi okundu; bit 0 SHT31 hata, bit 1 prob hata
} sensors_reading_t;

esp_err_t sensors_init(void);
// Prob gücünü açar, ısınmayı bekler, okur (3 deneme), gücü kapatır.
esp_err_t sensors_read(sensors_reading_t *out);
```

Okunamayan değer `NAN` olur; düğüm onu `null` olarak gönderir.

### Uyku (deep sleep)

- 24 saatlik tampon (`baseline_t`, 2,3 KB) uyku boyunca RTC belleğinde
  (`RTC_DATA_ATTR`) kalmalı.
- Uykudan önce prob gücü (MOSFET) kapalı, GPIO durumu sabitlenmiş olmalı.

### Rüya'dan beklenenler

1. Sürücü bileşeni (`CMakeLists.txt` ile), PC'de ya da ESP'de testi ve
   pin bağlantılarını gösteren kısa bir README.
2. Probun Pi'deki eski kayıtla aynı değerleri verdiğinin kontrolü
   (register sırası: önce sıcaklık, sonra nem).
3. Akım tablosu: aktif (240 MHz), Wi-Fi gönderim, light sleep, deep sleep;
   prob açık/kapalı. Çip ve kart ayrı yazılmalı.
4. Ortak GND: 12 V adaptör, ESP ve RS485 modülü aynı toprakta.

## 4. ML (Derya) — `firmware/components/tinyml/` ve `cloud/`

### Model ne yapar

Baseline ile **aynı girdi ve çıktı**:
- Girdi: son 24 saat, 288 örnek × 2 kanal (VWC, VPD), 5 dakika aralıklı
- Çıktı: sonraki 3 saat, 36 adımlık VWC tahmini

Karar kuralı modelin dışında, ortak (bölüm 1). Böylece aynı pencerelerde
baseline, Chronos ve TinyML doğrudan karşılaştırılır.

### İki teslim

1. **Float model (bulut için):** Python'da çağrılabilir bir fonksiyon:
   ```python
   def predict(vwc_24h, vpd_24h) -> vwc_next_3h   # (288,), (288,) -> (36,)
   ```
   Bulut servisi bunu `"model": "tinyml_float"` olarak çalıştırır.
2. **int8 model (ESP için):** TFLite Micro dosyası (`model_data.cc`), giriş/çıkış
   ölçekleme bilgisi ve C sarmalayıcı:
   ```c
   int tinyml_predict(const float vwc[288], const float vpd[288], float out[36]);
   ```
   Başlangıç noktası: `experiments/tinyml_budget/` (aynı biçimde bir model
   ESP'de 13 KB arena ile 5,1 ms'de çalıştı).

### Veri ve bölme

- Test seraları: **MuGrow ve AgriFusion** (staj sonuçları buradan; doğrudan
  karşılaştırma için). Eğitim: IDEAS, TomatoNuts, Trigger, Reference.
- Kalan dört sera gelene kadar: MuGrow/AgriFusion içinde **zamana göre**
  bölme (ilk %80 eğitim, son %20 test, arada 24 saat boşluk). Rastgele bölme
  yok: ardışık pencereler 287/288 örneği paylaşıyor.
- Ek test: okul serası kaydı (29 Eylül – 26 Ekim), "hiç görmediği saha".
- Açık: kuruma olayı ve FC tanımı (baseline ile aynı olmalı).

### Derya'dan beklenenler

1. Pencereleme ve etiketleme kodu; aynı pencereler baseline için de kullanılabilmeli.
2. İlk basit model ve test sonucu (doğruluk, zamanlama hatası).
3. Float modelin `predict()` fonksiyonu → bulut.
4. int8 model + kayıp ölçümü (float ile farkı) → ESP.

## 5. Bulut (Zehra) — `cloud/`

- Çalıştığı yer: bulut VM'i, Docker içinde (Mosquitto + servis).
- `service.py`: `reading` dinler, düğüm başına 24 saatlik tampon tutar, her
  modeli çalıştırır, model başına bir `cloud_decision` gönderir.
- Modeller: `baseline` (staj Python referansı), `chronos` (Chronos-tiny),
  `tinyml_float` (Derya'dan gelince).
- **Dijital ikiz:** fizik modeli her okumayla güncellenir; "sulamasaydık ne
  olurdu" senaryoları ve tahmin–ölçüm farkıyla anomali tespiti.

## 6. Takvim (öneri, 2 Aralık ara değerlendirmesine kadar)

| Hafta | Zehra | Rüya | Derya |
|---|---|---|---|
| 26 Eki | Bulut sunucusu (VM + Docker + Mosquitto), bulut servisi: baseline + Chronos, Mac→bulut köprüsü | Sensörleri al, prob ve SHT31'i ESP'de oku | Pencereler + etiketler |
| 2 Kas | ESP → gateway → bulut → ESP, gerçek kararlar ve internet üzerinden gecikme | `sensors_read()` + güç anahtarlama | İlk basit model, ilk sonuç |
| 9 Kas | Kesinti testleri; sürücüyü döngüye bağla | Deep sleep döngüsü, ilk akım tablosu | Float model → bulut |
| 16 Kas | Logger'ı Pi'ye taşı | Akım ölçümleri (edge / cloud modu) | int8 model → ESP |
| 23 Kas | Dijital ikiz ilk hali; entegrasyon | Ölçüm tamamlama | Karşılaştırma tablosu |
| 30 Kas | Ara değerlendirme hazırlığı (hep birlikte) | | |

Tarihler ekip toplantısında birlikte netleştirilecek.

## 7. Kurallar

- Herkes kendi klasöründe ve kendi branch'inde çalışır; derleniyorsa `main`'e alır.
- Arayüz değişecekse (fonksiyon imzası, mesaj alanı) önce bu doküman
  güncellenir, sonra kod.
- `firmware/sdkconfig.local` asla commit edilmez.
