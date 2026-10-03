# Uzun CPU OMR akışı

## Kapsam

`mode-stream.v1`, LSH, OPTICS veya SOM ile bir modeli bir kez eğitip kronolojik
girdiyi küçük parçalar halinde işler. Her parçada aynı model, nearest neighbor
referansı, sensör residual'ları ve OMR kullanılır. Alarmın bekleme/histerezis
durumu bir sonraki parçaya aktarılır.

Sentetik kaynak yeniden üretilebilir süreç verisidir. Varsayılan girdi
192 eğitim ve 8192 izleme satırıdır. Eğitim en fazla 4096; izleme 65536;
parça 64 satır; toplam parça sayısı 1024 ile sınırlıdır. Sentetik zaman ve
rastgele sayı dizisi parça sınırında yeniden başlatılmaz.

Bu, sonlu ve **puanlanmayan tanı akışıdır**. `scoring_available=false` kalır.
Bir araştırma önerisi, bağımsız doğruluk ölçümü, KEEP kararı veya modelin
kendi kendine öğrenmesi olarak sunulmaz. Yeni model eğitiminin sürüm
değiştirmesi ayrı teslimattır.

## Çalıştırma yolu

Mevcut Lab API ve Director kuyruğu kullanılır. Yeni bir GPU tahsisçisi yoktur;
bu akış CPU sandbox'ında çalışır. GPU model çağrısı gerekmez.

1. `POST /v1/mode-stream-inputs/synthetic`: senaryo, seed, eğitim/izleme/parça
   boyutları ile değişmez girdi oluşturur.
2. `POST /v1/mode-streams`: `input_sha256`, `configuration`, `wall_seconds` ve
   `idempotency_key` ile koşuyu kuyruğa alır. Belirsiz yanıttan sonra aynı
   işlem aynı istek kimliğiyle tekrar gönderilir.
3. Mevcut Director `dispatch-one`/kuyruk servisi işi yürütür.
4. `GET /v1/runs/{run_id}/mode-stream` kalıcı ilerlemeyi verir.
5. `GET /v1/runs/{run_id}/mode-stream/chunks/{index}` yalnız tamamlanmış bir
   parçayı döndürür. Tüm akış tek büyük yanıta yüklenmez.
6. Mevcut `POST /v1/runs/{run_id}/stop` ve terminal `/report` yolu kullanılır.

Arayüzde **Çalışma modları → Uzun CPU OMR akışı** bölümünden yöntem ve
hiperparametreler seçilir. Deney ayrıntısı OMR ile gerçek/NN sensör eğrilerini,
residual tablosunu ve seçili parçanın JSON dışa aktarımını gösterir.

## PostgreSQL kaynağından akış

Mevcut `LAB_MODE_SOURCE_CATALOG_FILE` kataloğuna kaynak bazında `stream`
izni eklenir. Katalog ve DSN dosyaları yalnız servis kullanıcısının
okuyabildiği normal dosyalar olmalıdır (`0600`, symlink/hardlink olmadan).
DSN, kaynak tablosuna/view'a yalnız SELECT yetkisi olan ayrı PostgreSQL
kullanıcısını kullanmalıdır. Bağlantı bilgileri tarayıcıya gönderilmez.

Örnek katalog girdisi; yollar, principal owner kimliği ve tablo kurumun
kendi ortamında belirlenir:

```json
{
  "schema": "mode-source-catalog.v1",
  "sources": [{
    "selection": {
      "source_id": "plant-telemetry",
      "source_version": "telemetry-view.v1",
      "table": "telemetry",
      "timestamp_column": "observed_at",
      "entity_column": "asset",
      "row_limit": 4096,
      "sampling_seconds": 2.5,
      "units": ["degC", "bar"]
    },
    "sensors": ["temperature", "pressure"],
    "owners": ["your-local-principal-owner"],
    "dsn_file": "/absolute/private/path/source.dsn",
    "stream": {
      "enabled": true,
      "schema": "public",
      "row_limit": 65536,
      "capture_seconds": 20,
      "local_export_allowed": false
    }
  }]
}
```

Eski snapshot'ın 4096 satır izni değişmez. `stream` yoksa veya kapalıysa
uzun veri alımı reddedilir. Akışın `row_limit` değeri **eğitim + izleme
toplamını** sınırlar; en fazla 65536'dır. Arayüzde kaynak, sensörler, UTC
aralığı, varlık, eğitim satırları ve yöntem seçilir. İlk eğitim prefix'i
kullanıcının seçtiği referanstır; etiketsiz DB'de bağımsız olarak sağlıklı
doğrulandığı iddia edilmez.

`POST /v1/mode-stream-inputs/database`, kalıcı `idempotency_key` ile bu
seçimi alır. Aynı anahtar ve seçim aynı girdiyi döndürür; farklı seçim
`409` olur. Ardından yukarıdaki değişmeyen `/v1/mode-streams` yolu kullanılır.
Başka owner, girdi hash'ini bilerek koşu başlatamaz.

Veri tek REPEATABLE READ/READ ONLY transaction'da, sınırlı server cursor
ile alınır. Zaman sütunu TIMESTAMPTZ olmalıdır; oturumun zaman bölgesi UTC'ye
alınır. `[start_utc, end_utc)` aralığı kullanılır. Fazla satır sessizce
kesilmez; eş timestamp ve kronoloji bozukluğu reddedilir. `mode-stream-input.v2`
gerçek UTC zamanlarını, sensör sırasını/birimleri, seçimi, kaynak tanımı ve
frame/timeline hash'lerini saklar. Sentetik v1 girdilerinin anlamı değişmez.

Arayüz zaman aralıklarını korur. Beklenen cadence varsa onu aşan aralıklar
boşluk olarak işaretlenir; jitter da bu sayıya dahildir. Cadence bilinmiyorsa
uydurulmaz. Alarm dwell sayacı gözlenen satır sayısıdır; zaman boşluğunda
gizlice reset veya yeniden örnekleme yapılmaz. Model sayısal sensör verisini
alır; UTC dizisi güvenilir girdi/gösterim katmanında kalır.

Veri alma süresi bağlantı, sorgu, yazım ve doğrulama için tek özgün bütçedir
(katalogda 1–30 saniye). Tarayıcı iptali bu bağlantıyı kapatır; sunucu yalnız
bu veri alımının sorgusunu/işçisini sonlandırır. Veri alma iptali deney
oluşturmaz. Kuyruğa alınmış deney için mevcut durdurma düğmesi kullanılır.
Yerel JSON indirme ayarı yayımlama veya eğitim izni vermez; özel veri için
teknik bir kopyalama engeli olarak sunulmaz.

## Durum ve toparlanma

- `stop_requested` bir istektir; terminal kapanış veya kaynak bırakımı kanıtı
  değildir. Arayüz kapanış doğrulanana kadar bunu ayrı gösterir.
- Mevcut Director owner/generation, özgün süre sınırı ve checkpoint işlemleri
  kullanılır. Yeni işçi, eski işçinin gecikmiş cevabını kabul etmez.
- Her fit/predict aşaması en fazla 60 saniyedir. Özgün çalışma süresinin son
  30 saniyesi kapanış/rapor için ayrılır; stop isteği süre bütçesini yenilemez.
- İlerleme yalnız doğrulanmış ve kalıcı kayda bağlanmış parçalarla ilerler.
  Dosyaya yazılıp henüz kayda bağlanmamış çıktı tamamlanmış parça sayılmaz.
- Fit artefaktı, model, girdi, sıra numarası, alarm durumu ve önceki parça hash'i
  devam işleminde korunur. Host fitted pickle içeriğini açmaz.
- Bağımsız Scorer terminal tanı raporunu yayımlar; tanı raporu puan üretmez.

## Kabul durumu

Uygulama `feat/omr-stream-v1` dalındaki ayrı Scientist çalışma ağacındadır;
açık arayüze dağıtılmamıştır. 2026-10-02 PostgreSQL teslimi:

| Kontrol | Sonuç |
|---|---|
| Gerçek taşıma | Console → Lab API → ayrı PostgreSQL; SELECT-only kaynak kullanıcısı |
| Veri kapsamı | Üretilmiş süreç verisi; 192 eğitim + 4224 izleme satırı, üç sensör |
| Akış | LSH, 66 × 64 satır, 227,58 s; bağımsız `completed` tanı raporu |
| Kaynak zamanı | Europe/Istanbul DB oturumundan gerçek UTC; 2,5 s cadence, kesirli zaman ve parça sınırındaki boşluk korunuyor |
| Veri alımı | 1,46 s; owner, idempotency, eş zaman ve fazla satır reddi doğrulandı |
| Gerçek HTTP iptali | Etkin PostgreSQL `FETCH` gözlendi; socket kapatıldıktan 0,174 s sonra kaynak bağlantısı yok |
| Özgün timeout | 1 s bütçe 1,015 s içinde HTTP 504; kaynak bağlantısı yok; başarısız alımlar deney oluşturmadı |
| Kapanış | Director inactive/PID 0; geçici API/console süreçleri kapandı; ayrı PostgreSQL kaldırıldı |
| Arayüz | TypeScript/Vite exit 0; gerçek HTTP console proxy kullanıldı; görsel tarayıcı kabulü yapılmadı |

İlk gözlemde iptal sonrası 5,022 s kapanış, sabit sorgu timeout'una denk
geldiğinden iptal kanıtı kabul edilmedi. Gerçek ASGI disconnect izleyicisi
düzeltildi. Ayrı capture-only tekrarında 0,118 s, son tam akışta 0,174 s
ölçüldü; ikisi de 5 s sorgu timeout'undan önce kapanır.

Son kalite kapısında **3198 passed, 7 skipped, 177 GPU/live deselected**;
Ruff/Pylint/Bandit/pytest/mypy/wheel build/wheel import komutlarının tamamı
exit 0. İlk wheel denemesi çalışma ağacındaki gereksiz ikinci `image.lock`
nedeniyle durdu; başarısız kayıt korundu. Kopya kaldırılıp tam kapı yeniden
geçti; tek kaynak `ops/sandbox-image.lock` korundu. Bu dosyanın kaldırılması harness hash'ini değiştirir; gerçek kabul
hash'i ile teslim hash'i ve tam fark kanıtta ayrı kaydedilir.

[PostgreSQL ölçümleri, kaynak hash'leri ve sınırlar](review-evidence/mode-stream-postgres-20261002.json).

### Önceki sentetik akış ve koşu durdurma kanıtı

`08bd136` commit'inde tamamlanan önceki CPU kabulü korunur:

| Kontrol | Sonuç |
|---|---|
| Gerçek akış | 192 eğitim + 4352 izleme satırı; LSH, 68 parça, 240,78 s; `completed` |
| Tek model/fit | İlk ve son parçada aynı hash; son global satır indeksi 4351 |
| Gerçek inflight durdurma | Çalışan predict konteyneri gözlendi; stop ve tekrar stop; bağımsız `stopped` raporu |
| Stop gecikmesi | Stop kaydından rapor doğrulamasına 4,53 s; ikinci koşu toplam 10,46 s |
| Kapanış | İki Director inactive/PID 0; gözlenen konteyner yok; P1 kaydı yok; ayrı PostgreSQL kaldırıldı |
| Gerçek PostgreSQL rolleri | 56 kontrol geçti |
| Genel kalite kapısı | 3103 passed, 7 skipped, 177 GPU/live deselected; yedi komut exit 0 |
| Arayüz | TypeScript/Vite exit 0; gerçek HTTP Lab API'ye giden console proxy kullanıldı |

[Kaynak, yapılandırma, rapor hash'leri ve sınırlar](review-evidence/mode-stream-cpu-20261002.json).
Kanıt sentetik CPU tanı akışına aittir; Scorer raporu doğruluk puanı üretmez.
Tarayıcıda görsel kabul, bu akışın gerçek crash/resume denemesi, gerçek endüstriyel
veri tabanı kabulü, otomatik model iyileştirme ve gerçek AOS/GPU devri çalıştırılmadı.
PostgreSQL taşınması gerçek olsa da bu teslimdeki satırlar üretilmiş veridir.
Bu teslim OM.3'ün sonlu CPU akışı bölümünü kanıtlar; tüm OM/M0 kabulünü kapatmaz.

## AOS oturumuna kısa aktarım

Ana Scientist kaynak pini `55c5300`, gözlenen AOS `ed6e857` ve mevcut AOS
control v2 sözleşmesi korunuyor. Yeni `mode-stream.v1` uçları şimdilik yerel
principal ile kullanılır; AOS-origin erişim eklenmedi. Bu CPU dilimi için GPU
tahsisi yoktur. Native v8 kaynak/config teyidi bekleniyor; ayrı dalın imajı veya
hash'leri mevcut AOS paketine kendiliğinden taşınmamalıdır. GPU kabulünün tek
yürütücüsü Scientist oturumu olmaya devam eder.
