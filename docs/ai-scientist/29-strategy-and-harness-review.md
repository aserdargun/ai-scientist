# 0.29 — Strateji ve harness kimliği entegrasyonu

2026-09-26. İki Luna 6 / high çalışma kopyasından sekiz dosya byte eşliğiyle
alındı. Üçüncü iş kolundaki holdout değişiklikleri bu sürüme alınmadı.

## Uygulananlar

- `lab/director/strategy.py`: dokuz hareket için Beta Thompson seçimi,
  tamamlanan deney başına 0,97 çürüme, ilk ziyaretler ve her 20 seçimde
  bütün hareketlerin kapsanması. Sürümü belirli PRNG, canonical JSON,
  hash doğrulaması ve kalıcı bekleyen seçim sözleşmesi aynı öneri tekrar
  denendiğinde yeniden örneklemeyi önler. On bir odaklı test; sabit RNG
  vektörü, dağılım kontrolü, kapsam, ödül/abandonment ve kayıt bozulması
  kontrollerini içerir. **Director döngüsüne bağlanması hâlâ açık.**
- `executor.py` ve `runner.py`: kaynak veya imaj kimliği değiştiğinde
  `REJECT(harness_hash_mismatch)` sonucu. Kontrol tamamlanmış seed
  önbelleğinin öncesinde/sonrasında ve sandbox çağrısından önce yapılır.
  Bu ret trajectory içinde aday kalitesi etiketi olarak sunulmaz.
  Gerçek fingerprint işleviyle test edilen tek bayt değişikliği yalnız
  geçici kaynak kopyasında yapılır; çalışma klasörü değiştirilmez.

M0.2'nin tam kabulü verilmedi. Test, fixture içindeki iki ardışık önerinin
ret sonucunu ve sıfır Docker/Scorer çağrısını doğrular. Gerçek PostgreSQL
terminal kayıtları, tüm CLI yeniden başlatma yolu ve commit sonrası döngü
checkpoint'ine dönme yolu ayrıca doğrulanmalıdır. CLI baseline kalibrasyon
kontrolü değişmiş kaynak pinini hâlâ döngüden önce reddeder; kayıtlı pin
değiştirilmedi ve geçmiş terminal kararlar yeniden yazılmadı.

## Gerçek kalite kapısı

`data/runtime/parallel-m0/integration-029/run-bound-gate.py` ortak CPU
kilidini servis başlamadan önce aldı. İmaj ve kalite servisleri 3 GiB RAM,
sıfır swap, iki CPU ve 128 task sınırıyla çalıştı.

- **Session 92987 / gerçek exit 0**; imaj 8,305 sn, kalite 49,047 sn.
- **372 passed, 7 skipped, 13 deselected**; strict mypy **84 kaynak**;
  Pylint **9,40/10**. Yedi kalite komutunun tamamı exit 0.
- İmajda **93 runtime dosyasının byte eşliği** doğrulandı.
- Harness 0.29.0 / 76 dosya:
  `3be08c30da6132aada7d2bba0d8211e19d46c34eedd85f3349356a917031dd91`.
- İmaj:
  `sha256:617b92bb8759ce2f38245b48030dd42ee384077aa2260163e1ffbfc8f943b8f4`.

Kaynak/çıktı hash'leri
`review-evidence/parallel-integration-029-quality-gate-binding.json`
içindedir. M0.2 ajanının dar mypy komutu kendi değişen Director dosyalarını
kapsamıyordu; ana kalite kapısının 84 kaynaklık komutu bunları da kontrol
etti. Önceki fixture/komut hataları teslim kayıtlarında korunur.

## Paralel işler ve kabul sınırı

AOS V3 sürücüsünün girdi ve CPU hazırlığı saklandı. Kaynak incelemesinde
temizlik hatasının başarılı çıkışa dönüşmesi, erken başlangıçta eksik
süreç nesli kaydı ve geçmiş GPU ticket'larının birlikte ilerleme sayılması
bulundu. Model çağrısı öneri satırından önce gerçekleştiğinden gözlem
aralığı da düzeltilmelidir. Bulgular
`review-evidence/aos-lab-coexistence-v3-root-review.md` içindedir; V3 gerçek
kabul için hazır değildir. V4 ayrı hazırlanıyor; canlı AOS değiştirilmedi.

Kabul özeti **10 geçti / 8 kısmi / 4 açık** olarak kaldı. Holdout,
stratejinin üretim döngüsüne bağlantısı, baseline aşama devri, EXPLORE,
public/model araştırması, gerçek eğitim ve AOS ile birlikte çalışma
doğrulamaları devam ediyor. GPU çalışması için bekleyen AOS koordinasyonu
CPU geliştirmesini durdurmuyor. Remote/auth ve PR gereksinimi açık.
