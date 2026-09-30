# Director yeniden başlatma kapsamı — 2026-09-27

GPT-6 Astra / high kaynak incelemesi; ana kaynak `d70dffc`, runtime 0.30.0.
Bu belge uygulama veya çalışma kanıtı değildir. Spec §3.3.1–3.3.2'deki
aynı araştırmaya kaldığı yerden devam etme ve sınırlı altyapı tekrar hakkı
M0 kapsamında açıktır.

## Mevcut davranış ve eksikler

| Kaynak | Gözlenen durum | Gerekli değişiklik |
|---|---|---|
| `lab/cli.py:549`, `:843`; migration `0016_director_recovery.py:19` | Dispatcher yalnız `queued` kabul ediyor; run başına ilk süreç sahibi değişmez. | İlk sahip kaydını koruyan nesil geçmişi ve atomik güncel sahip seçimi. |
| `lab/cli.py:259`, `:1241` | Ham `lab run` yolu `running` durumunu kontrol ederek yeniden giriş yapabilir; kayıtlı süreç sahibini ve global dispatch slotunu doğrulamıyor. | CLI ve kuyruk yürütmesini aynı yetkili sahip kontrolünden geçir. |
| `lab/director/journal.py:103`; `ledger.py:55` | Heartbeat ile sonraki DB mutasyonu ayrı bağlantılarda. | Her mutasyonun commit işlemi içinde beklenen sahip neslini doğrula; sahip değişimiyle aynı kilit sırasını kullan. |
| `lab/director/runner.py:793` | Altyapı tekrar sayacı görev başına bellekte; yeniden başlayan süreç hakkı sıfırlayabilir. | Deney sınırındaki tek tekrar hakkını ve bütçe + iki dakika uzlaştırmasını kalıcı tut. |
| `lab/director/executor.py:189`; `runner.py:1190` | Scorer commit'i ile ölçüm checkpoint'i arasında guard/süre kökeni kaybolabilir. Çıplak skor satırı haklı olarak yeterli sayılmıyor. | Scorer'a göndermeden önce tam değerlendirme kanıtını kalıcılaştır; sonuçla kimlik ve hash üzerinden birleştir. |
| `lab/director/budget.py:237` | Restore, geçmiş elapsed değerini koruyor fakat kesinti süresini saymadan monoton saati yeniden başlatıyor. | İlk çalışmada sabitlenen mutlak run son tarihini ve tüketilmiş rezervasyonları koru. |

Mevcut hash doğrulamalı checkpoint, baseline ve tamamlanmış seed geri okuma,
öneri niyeti, döngü durumu ve commit edilmiş terminal öneriyi ilerletme
yolları korunacak. Bu bileşenler tek başına güvenli bir yeni Director süreci
başlatma yolu sağlamıyor. Mevcut `recovery.py` stop/finalize yolunu koruyor;
araştırmaya devam etme işlemi olarak sunulmuyor.

## Uygulama sınırı

1. Aynı run ve değişmez araştırma isteği için kalıcı recovery niyeti,
   süreç nesli geçmişi ve güncel sahip göstergesi ekle. İlk sahip kaydını
   silme veya üzerine yazma.
2. Uzlaştırıcı, önceki PID/başlangıç/boot/unit/InvocationID/cgroup ve
   sahipli çocuklarının kapandığını doğrulasın. Global kabul slotu ve run
   kilidi altında yalnız bir yeni nesil kurulsun. Stop, terminal durum,
   değişmiş suite/harness/image/provider veya belirsiz çocuklar yeniden
   başlamayı engellesin.
3. Director, Planner iş kabulü ve geç gelen finalization/failure yazıları
   dahil bütün mutasyonlar işlem içinde nesil doğrulasın. Otomatik kuyruk
   açılışı uygun yarım kalmış araştırmaları bulabilsin.
4. Tamamlanmış skorlar ve terminal belgeler tekrar yürütülmeden kullanılsın.
   Altyapı tekrarı öncesi kalıcı hak ve süre tüketimi yazılsın; aday çökme
   sayacı, Thompson RNG/posterior veya öneri sırası hatalı ilerlemesin.
5. Mutlak son tarih, kalan token/duvar saati ve açık rezervasyonlar yeni
   süreçte doğrulansın. Kesinti yeni bütçe yaratmasın.

0022/0023 kalibrasyon için ayrılmıştır; baseline işi 0024 kullanabilir.
Restart migration'ı entegrasyon sırasında bunlardan sonra numaralanacak.
Bir çalışma ağacının migration dosyaları diğer worker tarafından değiştirilmez.

## Gerekli kabul kanıtları

- Baseline sırasında, Scorer commit'inden sonra ve terminal belge commit'inden
  sonra gerçek süreç kaybı: aynı run devam eder, iş/skor/belge çoğalmaz.
- İki recovery adayı: yalnız bir yeni sahip; DB bağlantısını kaybeden eski
  süreçten geç yazım veya completion reddedilir.
- Stop/recovery yarışı: stop sonrasında araştırma yeniden başlamaz.
- Tek altyapı tekrarı sırasında çökme: sonraki süreç ikinci tekrar hakkı alamaz.
- Kesinti ilk son tarihi aşar: yeni iş başlamaz.
- Eksik/bozuk köken veya değişmiş çalışma kimliği: çalıştırma reddedilir.

## `lab baseline` kapsam kararı

Brief 3k ve spec CLI envanterindeki bağımsız komut; önceden kabul edilmiş
baseline amacı, kendi sınırlı bütçesi, tam 3 algoritma × 3 seed × güvenilir
görev kümesi ve doğrulanmış terminal kalibrasyon raporuyla uygulanabilir.
Normal `lab run` baseline → araştırma akışını korur. Önceden kabul edilmiş
bir araştırma isteği baseline sonunda erken bitirilmez; başka run'ın
kalibrasyonu taşınarak bütçe sıfırlanmaz.

Özel `baseline-handoff-029` ağacındaki ADR0020 öneri durumundadır; ayrı
komuttan aynı run'a aşamalı devam etme seçeneği bağlayıcı kullanıcı şartı
değildir. Bu karar yukarıdaki araştırma restart şartını kaldırmaz.
Kaynak incelemesi: [Astra inceleme kaydı](review-evidence/drain-before-cas-probe-030-source-review.json).

Kabul sayıları değişmedi. GPU/AOS koordinasyonu, gerçek veri/model deneyleri,
tam kalite ve PR kapıları ayrı açık maddeler olarak korunuyor.
