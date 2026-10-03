# Native R5: kalibrasyon, G1/G2 fencing ve eksik primary kabulü

Scientist HEAD `01dab7d97a97c4402ce416cebc6506f22834c131`; readonly AOS
HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`. Ana servisler
ve AOS değiştirilmedi; push/deploy veya GPU koşusu yapılmadı.

## Geçen kısım

Yeni ayrı CPU fixture ve normal SQL0037/dört rol kullanıldı. Özgün istek
1800 saniye / 1 deney / 0 model token; hiçbir alan sonradan değiştirilmedi.
Üç gerçek baseline deney, 36 gerçek bağımsız Scorer hücresi ve donmuş
kalibrasyon oluştu. Fiziksel kimliği doğrulanmış yalnız kendi G1 sürecine
pidfd SIGKILL gönderildi; normal recovery/drain ile G2 sahipliği oluştu.
Özgün calibration hash ve execution/deadline korundu. G1'in gecikmiş
kapanış girişimi reddedildi ve G2 snapshot'ı değişmedi. 22 ara kontrol geçti.

## Başarısız ve çalıştırılmayan kısım

Genel koşu **exit1**, 1296.729585 saniye; kaynak hash'leri değişmedi.
G2 loop `budget_exhausted`, dispatcher `RuntimeError`, ledger `failed`,
report NULL. Öneri veya primary Scorer claim'i oluşmadı; inflight stop,
terminal stopped report, cleanup retry ve gerçek GPU/AOS kabulü çalışmadı.

Test isteğinin sıfır token bütçesi bir fixture hazırlık hatasıdır:
DirectorLoop, grid provider dışındaki provider'larda remaining_model_tokens<1
ise öneri öncesinde durur. FakeLLM de bu kontrol ve normal episode
rezervasyonuna tabidir. S1 rezervasyonu 18432, S2 rezervasyonu 24576 token;
S2 ayrıca 720 saniye gerektirir. Gelecek koşu bütçesi kalibrasyon süresi,
normal episode rezervasyonu ve cleanup payını *başlamadan* doğrulamalı.
Sıfır bütçe guard'ı bypass edilmez. Bu açıklama primary'nin neden
başlamadığını gösterir; dispatcher RuntimeError'ın bütün finalizer nedenlerini
kanıtlamaz. R5 deadline/bütçe/results reset edilmez.

## Cleanup kanıtı

Ayrı bounded root kontrolü exit0: G1/G2 exact sahipleri ölü, cgroup'ları boş,
tüm tarihsel Scorer işçileri fiziksel olarak quiescent, queued/running job0.
Parent/API inactive, original PID'ler yok ve cgroup boş. Yalnız fixture PG
kimlik/label doğrulamasından sonra durduruldu; depolama ve failed ledger
korundu. Bunlar GPU allocation/release veya terminal stopped kanıtı değildir.

## Kaynak incelemesinde kalan sorunlar

- Drained proof sonrası ledger commit başarısızlığında yeni recovery worker
  kimliği retry'ı engelleyebiliyor; kısmi CAS crash sonrası gerçek W1/W2
  invocation ancestry korunmalı, eski InvocationID taklit edilmemeli.
- İlk primary hücrede stop sonrası planlı ama admitted olmayan hücreler için
  dürüst infrastructure_unattempted completions eksik olabilir. Orijinal
  task planı korunmalı; skor/job uydurulmadan final inventory seal sırası
  tamamlanmalı.

Bu iki kaynak bulgusu R5'te native olarak denenmedi. Regresyon-first sonraki
aday çalışması ayrı sürüyor. Kabul toplamı 11 geçti / 7 kısmi / 4 açık.
[Makine özeti](review-evidence/native-stop-r5-partial.json).

## AOS'a aktarılacak beklenti

Aday `aos-scientist-runtime.v1 / wire1` henüz ortak runtime kabulü değildir.
Gerçek worker kaynakları mevcut, ancak checkout dirty/untracked ve capability
teyidi eksik olduğundan source preflight exit2/admissionfalse. Birleştirilecek
sözleşme exact principal/owner/generation, acquire, cancellation/status/reconcile,
timeout, drain ve trusted release/quarantine kanıtını bağlamalı. AOS idle veya
HTTP stop ACK GPU release değildir. Entegre GPU koşusunu yalnız Scientist
oturumu, existing scheduler rezervasyonu ve çakışmayan canlı iş durumunda yapar.


CPU retry regresyonu uygulamadan önce **exit1: 2 failed / 2 passed** verdi:
ledger commit hatası sonrası drained retry ve bozuk terminal hash yanlışlıkla
W2 başlatıyor; yanlış owner invocation ve eski generation retleri geçti.
Ruff exit0. Bu kırmızı testler çözüm değildir. `drained` SQL durumuna bakarak
worker başlatmayı atlamak da tek başına yeterli değildir: job-unit quiescence
stop-recovery worker retirement kanıtını kapsamıyor. Yeni SQL0038 taslağı
actual unit/PID/ticks/boot/invocation ancestry ve retirement proof, planlı
missing-cell closure, sonra final inventory seal sırasını gerektirir; taslak
henüz çalıştırılabilir migration değildir. Ana runtime'a uygulanmadı.

## Sonraki aday: SQL0038 ve kapanış uygulaması

SQL0038 artık çalıştırılabilir migration adayıdır. Özgün primary planını,
gerçek recovery işçisi kimliklerini ve fiziksel retirement kanıtlarının
soy zincirini saklar. Çocuk işler kapandıktan sonra Planner, hiç admitted
olmayan planlı hücreleri `infrastructure_unattempted` olarak kapatır ve
terminal inventory'yi atomik mühürler. Drained retry yeni işçi başlatmaz;
aynı owner/generation/execution ve fiziksel kapanış yeniden doğrulanır.

Python uygulaması ve SQL kaynak incelemesi tamamlandı. Son zorunlu kalite
kapısı 2026-09-30 13:25 UTC'de **yedi komutun tamamında exit0** verdi:
1440 test geçti, 7 opt-in test atlandı, 120 GPU/live test seçilmedi.
Bu sonuç gerçek inflight stop veya GPU/AOS kabulü değildir. Yeni R6 koşusu
ayrı SQL0038 veritabanında 2400 saniye / 1 deney / 24576 token rezervasyonu
ile hazırlanıyor. Fake-json provider CPU fixture'ıdır; gerçek LLM kullanımı
veya ölçülmüş model token tüketimi olarak sunulmaz. R5 kayıtları değişmedi.
