# 0.28 eğitim bakımı ve 0.27 rapor/replay kabulü

Tarih: 2026-09-25. Eğitim kodu Luna/high tarafından ayrı worktree'de
hazırlandı; root incelemesi ve ana dal entegrasyonu ayrı kaydedilir.

## Gerçek 20 deney ve üretim CLI doğrulaması

Değişmeyen 0.27 kaynakları (`390a6c25e94e5da339457dd17e88749ed07c7795`)
ve özel `swapp_lab_m0_report_027_1e946abd` veritabanında üretim `lab run`
905,148 saniyede exit 0 verdi. Senaryo yalnız aday kaynaklarını sağlar;
skorları gerçek Docker fazları ve ayrı Scorer hesaplar.

- Run: `6ad4dec3-21df-4d3a-ab58-231da9f80a4e`.
- 3 baseline, 20 öneri, 80 Scorer sonucu.
- KEEP 1, KEEP_SIMPLER 1, DISCARD 5, REJECT 13.
- Aday çökmesi ve gerçek fit timeout'u; 23 experiment/trajectory çifti.
- 36 baseline ölçümü, seed 0–2 noise, seed 0–1 teyit ve parent lineage.

İlk hazırlıkta güncel weight provenance alanları eksikti; üretim işi
başlamadan doğrulama reddetti. V2 bu fixture metadata'sını düzeltti.
V2'nin üretim koşusu ve sekiz ledger kontrolü geçti; grafik kontrolü
baseline experiment belgesindeki henüz normalize edilmemiş `suite_score`
alanının null olması nedeniyle durdu. Üretim raporu zaten doğru biçimde
registered calibration içindeki üç seed ortalamasını kullanıyordu.
İki başarısız inceleme kaydı değiştirilmedi.

V3 aynı kalıcı koşuyu okur; deneyleri tekrarlamaz. Gerçek `lab report` ve
`lab replay` CLI'ları tekrar çalıştırıldı. **Session 82460 / exit 0,
15/15 kontrol**, 4,283 saniye:

- HTML hash ve tüm ledger sayıları aynı.
- 21 sequence/score noktası, 20 H/V merdiven basamağı aynı.
- 7 primary + 2 confirmed karar bloblardan tekrar hesaplanır.
- 13 scoreless terminal REJECT için immutable neden kaydı doğrulanır;
  guard'ın yeniden çalıştırıldığı iddia edilmez.
- Verdict ve delta/ci_low/noise `float.hex()` değerleri aynı;
  ölçülmemiş alanlar null kalır.
- Kuyrukta/running durumda bu koşuya ait Scorer işi kalmadı.

Komutlar, gerçek exit kodları, kaynak/imaj/driver/çıktı hash'leri:
`review-evidence/report-replay-twenty-027-v2-outcome.json` ve
`report-replay-twenty-027-v3-outcome.json`. Paylaşılabilir HTML:
`review-evidence/report-replay-twenty-027-v3.html`. Özel fixture ve
bloblar tekrar replay için saklanır; sırlar rapora alınmaz.

## Ek C Referee

Orijinal örneğin RNG sırası korunarak altı Referee örneği üretim
fonksiyonuyla çalıştırıldı. **6/6 / actual exit 0**. Verdict'ler ve
orijinal dört ondalıklı çıktılar aynı; kararlı ağırlık aritmetiği nedeniyle
orijinal formülden en büyük son-bit farkı `3.469446951953614e-18`.
Orijinal REJECT NaN değerleri review gereği strict JSON null olur.
Üretim kararının kendisiyle replay bit eşliği yukarıdaki gerçek koşuda
ayrıca doğrulanmıştır.

İlk verifier orijinal formülle gereğinden güçlü bit eşliği şartı koyduğu
için exit 1 verdi; v1 driver ve başarısız sonuç saklıdır. V2 yalnız
orijinal örneğin kendi beklentilerini ve review düzeltmesini doğrular.
Kanıt: `referee-appendix-c-027-outcome.json`.

Bu kanıtlar, mevcut üç kümülatif sadeleşme sınırı fixture'ı ile
**M0.8, M0.11 ve M0.12** koşullarını karşılar. Public veriyle araştırma,
yerel model, holdout ve AOS birlikte çalışma kabulü bundan çıkartılmaz.

## Eğitim bakım dilimi

`lab gpu lease TRAIN --noop` ve `lab doctor train --dry-run` için sabit
bakım principal'i, özel kalıcı ledger, exact-generation çocuk süreç
bağlama/boşaltma, shared GPU kuyruğu ve yeni SERVE S1 duman çağrısı
uygulandı. Dry-run yalnız 9B/4-bit/rank32/24.576 sentetik token/3 adım
profilidir; adaptör kaydetmez. Ayrıntı ADR 0014.

İlk dondurulmuş dokuz dosyalık yama ve hash'li teslim:
`review-evidence/training-maintenance/delivery.json`. Ayrı CPU kontrolü
18 test, Ruff, strict mypy, Pylint ve Bandit için exit 0 verdi. Gerçek
CPU transient-unit denemesi sonuç yazımı, exact-generation stop ve
cgroup kaybolmasını doğruladı. Bunlar eğitim veya VRAM ölçümü değildir.

Root incelemesinden sonra ayrı düzeltme teslimi geldi:
`training-runtime-corrections-delivery.json`. Parent unit'in çalışma
dizini/import-root'u sabit; systemctl sorgu hatası yokluk sayılmaz.
Çocuk süreç başlangıcı/gate/boşaltma süresi kalan GPU kirasına sığar;
kimlik bağlanırken heartbeat sürer ve model import gate'i öncesinde kira
tekrar doğrulanır. 24 odaklı test, strict mypy ve linter'lar exit 0 verdi.

Gerçek CPU servis/yol denemesinde `ProtectSystem=strict` host root sahiplerini
UID 65534 olarak gösterdi ve mevcut yol denetimi doğru biçimde reddetti.
Yalnız güvenilir kontrol parent'ı mevcut Director servis düzenine uyarlandı;
bu parent'ta mount namespace seçenekleri kaldırıldı. Yol yetkisi, principal,
CPU/RAM/swap/task/süre sınırları ve ayrı GPU çocuğunun offline/read-only
izolasyonu korunur. Son gerçek parent-context probe exit 0, tepe 11,6 MiB;
model import'u yok. Ortak üretim GPU DB yolu yoktur ve oluşturulmadı.

Root, testte monkeypatch edilen ledger yolunun gerçekten kullanılması için
`MaintenanceLedger(TRAINING_LEDGER)` çağrısını açık hale getirdi; test
kendi geçici ledger dosyasını doğrular. İlk teslim/supplement kanıtları
değiştirilmez. Yeni training kaynakları harness fingerprint kapsamına
alındı; trusted sürüm 0.28.0'dır. ADR 0015 ek gerekçeyi kaydeder.

**Açık:** gerçek TRAIN/noop→SERVE ve 24k QLoRA kabulü; GPU denemeleri
paralel canlı AOS çalışmasıyla koordinasyon gerektiriyor. Canlı AOS
dosyaları/servisleri değiştirilmedi.

## Ana kod kalite kapısı

**Session 69363 / gerçek exit 0**. İmaj oluşturma 5,885 saniye; tam kapı
46,415 saniye. Yedi komut başarılı: **359 passed, 7 skipped,
13 deselected**, strict mypy 83 kaynak, Pylint 9,40. İmajın 92 runtime
dosyası ana kodla byte düzeyinde aynı. Kapı boyunca kaynak değişmedi;
imaj oluşturucunun tek değişikliği yeni imaj digest kilididir.

- Harness: `9ad961a481052a95f424f7b96a5f4da91c4fe04c095e72a7fb5ae5d9635b9b6f` (75 dosya).
- İmaj: `sha256:55e326447f4fe2d4ebe477f59aa68bf899b5fb22527e8902d0904eeb775098fb`.
- Komut/log/kaynak bağı: `review-evidence/parallel-integration-028-quality-gate-binding.json`.
- Dondurulmuş tam çıktı: `evidence/quality-gate-parallel-integration-028-bound.json`.

Bu kapı holdout/strateji/AOS kollarının devam eden WIP kaynaklarını kapsamaz.
Remote/PR hâlâ yok; otomatik merge yapılmadı.
