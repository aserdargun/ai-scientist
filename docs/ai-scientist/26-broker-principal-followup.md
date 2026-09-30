# Broker süreç kimliği ve paralel kabul hazırlığı

Başlangıç: `221e5d94ceb17e7ef1f933f55db60e8d02987a02` / 0.25.0.

## Birleşen değişiklik

AOS broker'ı sabit Lab servis eşlemesiyle başladığında, API üzerinden daha
sonra oluşturulan araştırmanın canonical Director üst sürecini de mevcut
kuyruk kaydından doğrulayabilir. İstisna yalnız Lab sahibine ve tam run UUID
adına uygulanır; sabit `resolve()` davranışı korunur. Canlı PID, başlangıç
zamanı, boot kimliği, systemd MainPID/InvocationID ve `swapp-gpu.slice`
altındaki tam cgroup eşleşmelidir. Model alt süreci ayrı runtime bağıyla
izlenmeye devam eder. Keyfi eğitim servisi adları kabul edilmez.

Luna'nın ayrı kopyasında 27 hedefli test, Ruff ve mypy exit 0 verdi.
Root yamanın SHA'sını, test loglarını ve ana koda uygulama sonrası iki
dosyanın byte eşliğini doğruladı. Kanıtlar:
`review-evidence/lab-runbound-lab-principal-verify-review.json` ve
`review-evidence/lab-runbound-lab-principal-025-main-source.json`.
Harness sürümü bu trusted kaynak değişikliği için 0.26.0 oldu.
Tam imaj/kalite kapısı **session 96606 / exit 0**: 324 passed, 7 skipped,
13 deselected; strict mypy 78 kaynak, Pylint 9.35 ve yedi komut başarılı.
87 runtime dosyasında imaj byte eşliği doğrulandı. Yeni imaj
`sha256:63e019f316ccb9ea3cc8fe7e254d1f3b954b26a1520e8411151f10ac7bbb3747`.
İmaj yapımı yalnız pin dosyasını güncelledi; son kalite kapısı boyunca
kaynaklar değişmedi. Kaynak/komut/imaj bağı
`review-evidence/parallel-integration-026-quality-gate-binding.json`.

## Korunan gerçek kanıt ve açık işler

- Önceki sabit 0.25 kaynaklarında 27/27 gerçek public görev, robust-z
  seed 0 ile ayrı Docker ve Scorer akışından geçti. Bu sonuç üç algoritma
  × üç seed kalibrasyonu veya tam araştırma kabulü değildir.
- Aynı sürümün gerçek S2 öneri denemesi, ortak kuyruğa katılmayan canlı
  AOS llama-server nedeniyle kesildi. Yalnız sahipli Lab süreçleri
  durduruldu; model önerisi sonucu yoktur. Koordinasyon yanıtı bekleniyor.
- Holdout koluna boş, ayrı `swapp_lab_m0_holdout_025_f110d860` veritabanı
  0018 migration seviyesinde hazırlandı (exit 0). Mevcut veritabanları
  ve global roller/parolalar değiştirilmedi. Yeni 0019 ve gerçek API
  kabulü bu teslimin parçası değildir.
- Özel Lab çalışma kopyası, immutable model dosyaları ve araştırma
  profiline bağlı özel API registry hazırlandı. Servis/model çalıştırma
  ve gerçek AOS desktop kabulü henüz yapılmadı.
- Eğitim tanı/maintenance komutları ve holdout kancaları iki ayrı
  Luna/high kolunda hazırlanıyor; bu commit bunları tamamlanmış saymaz.
- Lab CLI'nin özel test kuyruk yolu ile broker'ın sabit production
  `~/.local/state/swapp-gpu/arbiter.sqlite3` yolu için uyum düzeltmesi
  hazırlanıyor. Ortak DB kimliği doğrulanmadan birlikte test başlatılmaz.

## Güncel guard kabulü

Sabit 0.26 kaynaklarında mevcut 19 sentetik saldırı/temiz aday kataloğu
üretim Docker yolunda tekrar çalıştırıldı: **session 1913 / exit 0,
19/19 vaka, 106 faz, 71,639 sn**. Temiz causal/stateful/seeded adayların
determinism ve causality farkları 0.0; eval istatistiği ve centered rolling
reddedildi. Tohumsuz fit/score RNG, literal görev/zaman/64 sayı, bozuk skor,
NRM zaman rampası ve uzun fit beklenen retleri aldı. Stateful dört score
çağrısının frozen fit hash'i aynı, konteynerleri ayrıdır. Kaynaklar sabit;
kendi konteynerleri ve çalışma dizini temizlendi.

Üst süreç 1 GiB RAM/1 CPU/swap 0; her sandbox 512 MiB/1 CPU ile sınırlıydı.
Ortak CPU test kilidi vakalar arasında bırakıldı. Tarihsel sürücü/çıktı
değişmedi; yeni wrapper ve hash bağı `guard-docker-026-outcome.json`
içinde bulunur. Bu kanıt §7.M0.4, M0.5 ve M0.7'deki belirtilen fixture
kabulünü kapatır. Public araştırma, genel ezber tespiti veya AOS/model
kabulü olarak sunulmaz.

Ek C [3] alarm örnekleri ayrıca üretim CPU fonksiyonlarında çalıştırıldı:
**6/6 kontrol / exit 0**. Dwell/release örneği ve üç PDM ile bir NRM
örneğinin skor/FA/doluluk değerleri bağımsız sabit beklentilerle
`float.hex` düzeyinde eşleşti. Hep açık PDM ve NRM alarmı ret üretmeden
0 puan aldı. `alarm-appendix-c-026-{review,outcome}.json` ve ayrı sürücü
kaynak/komut bağını saklar. Bu kontrol ile yukarıdaki gerçek Docker
dejenere/NRM-rampa vakaları birlikte §7.M0.6'yı kapatır.

22 kapının güncel durumu **7 geçti / 11 kısmi / 4 açık**.
