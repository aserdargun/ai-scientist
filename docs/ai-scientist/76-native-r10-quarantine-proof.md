# 76 — R10: committed CAS gözlemi, gözlemci hatası ve fiziksel karantina

30 Eylül 2026. **Terminal kabul başarısızdır.** Bu deneme R9'un başarılı
[güvenli stop kanıtını](73-native-r9-terminal-stop-proof.md) değiştirmez.
[Makine özeti ve ölçüm sınırları](review-evidence/native-stop-r10-quarantine.json)
gerçek çıkış kodlarını ve gözlem anındaki kaynak kimliklerini içerir.

## Geçen, kalan ve çalıştırılmayan adımlar

| Durum | Kanıt |
| --- | --- |
| Geçti | 36 gerçek CPU baseline hücresi; frozen calibration; gerçek G1 crash → G2 resume ve gecikmiş G1 reddi; çalışan primary Scorer claim'i sırasında typed stop ve ilk stop zamanını koruyan tekrar. |
| Geçti | Tam özgün iş/owner/generation bağında W1 transaction'ı commit oldu; job/drain/outcome/completion ve henüz mühürlenmemiş closure bağımsız okundu. |
| Başarısız | Özel gözlemcinin `original_remaining` çağrısı, `main` içindeki sonraki yerel `timedelta` import'u nedeniyle `NameError` verdi. Parent **exit 1 / 1270.183121527 s**; public kaynaklar değişmedi. |
| Çalıştırılmadı | Planlanan W1 pidfd crash; aynı recovery kimliğiyle W2 retry; drained rapor retry. Hata W1 sinyalinden önce oluştu. |
| Geçti | Bağımsız salt okunur fiziksel kontrol **exit 0 / 1.399950360 s**: 37 tarihsel Scorer, G1/G2, tek recovery işçisi ve sahip olunan API/parent kapalı; cgroup'lar boş; aktif iş 0. |
| Kaldı | Ledger `stop_requested`, closure `pending`, rapor NULL. Özgün 120 saniyelik pencere doldu. Aynı snapshot korunuyor; süre/bütçe/claim/retirement satırı yeniden yazılmadı. PostgreSQL inceleme için çalışır bırakıldı. |
| Çalıştırılmadı | Gerçek yerel model, GPU tahsisi/devri/release, VRAM ölçümü, AOS ortak kabulü ve eğitim. |

Bu fiziksel kontrol terminal ledger yaratmaz ve GPU release kanıtı değildir.
W1 doğal hata çıkışı, planlanan fiziksel crash kabulü olarak sunulmaz. Özel
gözlemcinin en küçük düzeltmesi ayrı candidate dosyasında tutuldu; başarısız
özgün dosya korundu. Düzeltilmiş candidate çalıştırılmadı. Yeni koşu başlatılmadı.

## Kaynak ve ölçüm bağı

- Scientist kaynak HEAD: `cc9511d4b449620b4bc1cc1a57ded41374eeb591`.
  AOS salt okunur HEAD: `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`, dirty.
  İki tracked diff hash'i makine özetinde gözlem zamanı ile kayıtlıdır;
  untracked dosyaları kapsamaz. Kontrol önerisinin ayrı hash'i de kayıtlıdır.
- Run: `6eaf72ba-2110-42c7-b5a2-0e8cd20245b8`. İzole SQL0040;
  ana runtime SQL0035 değişmedi. 484 dosyalı manifest:
  `81d25c5037299c7d9e4c8aa0560cc5dab6caaaa50f49401b3897a40a07d3b5c3`.
- Provider `fake-json`; dört sentetik EVT ailesi, her birinde 64 train / 96 eval.
  İstek sınırı 2400 saniye, 24576 model-token, bir deneydir. Token sınırı tüketim
  ölçümü değildir. Baseline ölçümleri tamamlanmış araştırma değildir.
- Gerçek model/quantization/context, VRAM tepesi ve GPU bekleme/devir/çalışma
  gecikmeleri **ölçülmedi**. Parent süresi CPU kabul sürücüsünün süresidir.

M0 sayısı değişmedi: **11 geçti / 7 kısmi / 4 açık**. Çoklu CAS suffix,
öğrenilmiş adaptör veya ortak GPU kabulü iddiası yoktur. Lisans/genel CI işleri
bu denemeden ayrı tutulur.

## AOS oturumuna aktarılabilecek kısa çıktı

Ortak kaynak temeli `aos-scientist-runtime.v1 / wire 1` korunuyor.
[75 kontrol önerisi](75-aos-control-contract-proposal.md) ayrı authenticated
capability/status/cancel/reconcile yüzeyini, immutable cancellation ve trusted
terminal release kanıtını somutlaştırıyor; **henüz anlaşılmış veya deploy
edilmiş değil**. Sonraki [77 kaynak teslimi](77-aos-control-source-delivery.md)
Scientist uygulamasını ekledi; gerçek bağlantı etkinleştirilmedi. AOS bu önerinin
son bölümündeki beş kararı ve gerçek caller /
deployment / profile pinlerini teyit etmelidir. GPU kabulünü yalnız Scientist
yürütür; idle/ACK/rapor veya bu CPU karantina kanıtı release yerine geçmez.
