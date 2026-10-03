# Native R9: gerçek CPU inflight stop ve temiz terminal kapanış

Scientist kaynak commit'i `957b4517e1200c07609952f71d5b6b670621d19a`;
AOS salt okunur HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Run `2506f0ca-462d-4637-baf0-422f06d83f71`, revision
`0040_attempted_stop_context_lock`. Yeni veritabanı ve yeni istek kullanıldı;
R7/R8 quarantine, owner, generation veya deadline kayıtları değiştirilmedi.

## Geçen kabul

| Adım | Gerçek kanıt |
| --- | --- |
| Baseline | Üç scored baseline / 36 bağımsız Scorer hücresi / frozen calibration; restart ve stop sonrası ölçümler birebir aynı. |
| Crash ve fencing | Exact G1 pidfd crash; normal G2 resume; gecikmiş G1 kapanışı reddedildi; özgün bütçe/deadline korundu. |
| Inflight stop | Gerçek primary claim running iken typed stop; tekrarlı stop ilk zamanı korudu. |
| Atomik terminal zincir | V2 full original plan dört hücre; bir admitted job; dört completion; üç infrastructure_unattempted. Proposal abandoned; closure drained; child/final hash'leri doğrulandı. |
| Recovery işçisi | Bir gerçek actor kaydı ve retirement; exact PID/start ticks/boot/unit/invocation/cgroup için fiziksel kapanış tekrar doğrulandı. |
| Rapor | Production API doğrulamalı stopped rapor; bağımsız strict experiment/trajectory readback; candidate decision/suite_score/outcome NULL, kalite hükmü verilmedi. |
| Cleanup | Aktif job0; G1/G2 ölü ve cgroup'ları boş; tüm tarihsel Scorer'lar quiescent; API/parent inactive/MainPID0; exact owned PG durduruldu, storage korundu. |

Observer **37 kontrolü geçti**; bounded parent exit0 / **1255.331418 saniye** /
kaynak değişmedi. G1 ve G2 waiter/owner çıkışları **1** olarak korunmuştur:
G2 cevabı automatic_stop_recovery=completed, dispatch=terminal, state=stopped
ve aynı rapor hash'ini içerir. Kontrollü owner başarısızlığı tüm komutlar exit0
şeklinde sunulmaz. Bağımsız cleanup son kontrolü exit0; ilk yardımcı kontrollerdeki
exit-code ve artifact-root varsayımları gerçek dispatch semantiği/production
artifact yoluna göre düzeltildi, başarısız receipt'ler silinmedi.

## Kimlikler ve ölçümler

- Frozen runtime source manifest (483 dosya):
  `ed3d44b6d8a54d4b0e5dc128bd7618194e8eee67289f6eab3de358d62e66525f`.
  Yalnız test kopyasında üç açık barrier/claim-hook farkı; production owner,
  SQL, artifact, Scorer veya bütçe guard'ları değiştirilmedi. Global P1 kilidi
  aynı inode4617128; ikinci scheduler veya GPU tahsis otoritesi kurulmadı.
- Rapor: `e942c3914b5abdefc365b1194a524d9abf9634506a43b51c105b377850c31673`.
- Terminal inventory:
  `2587029c7250276c0cc790098527a2ab2d633627693b06910627afa279e4fbc4`.
- Strict proposal document:
  `2158a67aeb7bfe0c2c0dc35687012e72a825e3559d956c9523cc6bcb68fd41e9`.
- Bağımsız cleanup helper:
  `8d411d7c5e4895d98db553c5609fd3e7787d9e96f38b627f112310f6f134ccb3`.
- Cleanup receipt: `cleanup-r9-r4-86c18cbac2b34121bd0487783349b01e`;
  genel koşu: `native-r9-f09bf76befbc4a9bbc10b4b5b45ad560`.
- Runtime bütçe: 2400s / 1 deney / 24576 token rezervasyonu; parent2700s;
  parent2GiB/1CPU/swap0, PG512MiB/0.5CPU, aggregate Scorer2GiB/1CPU.
- Dört sentetik aile, 256 train / 384 eval. Provider fake-json; gerçek model,
  quantization veya model context ayarı yok; tokenlar ölçülmüş LLM kullanımı
  değildir. GPU allocation yok; VRAM tepe, GPU bekleme/devir/drain gecikmeleri
  **ölçülmedi**. Eğitim veya öğrenilmiş adaptör kabulü yapılmadı.
- Teslim gözlemindeki tracked diff SHA-256: Scientist
  `3f5e95057c865b5b99e3f45e9408e219ee20d96ccb4f7fefdc420bafea80c80b`
  (yalnız canlı ilerleme JSON'u); AOS
  `b66dcd78eedb436bcfe62eb014431d3bd8d3afa3a6ed7576abbbcf0ee154f373`.
  Bunlar untracked dosyaları kapsamaz; seçili AOS source hash'leri [70](70-aos-runtime-readiness.md)'tedir.

## Açık ve çalıştırılmayan

- Gerçek partial-CAS recovery worker crash/retry ve drained proof sonrası ledger retry.
- AOS ortak `aos-scientist-runtime.v1` / wire1 capability-version-principal,
  cancellation/reconciliation ve trusted drain/release teyidi. Henüz ortak runtime
  admission yok; AOS idle/quiesce veya rapor GPU bırakma kanıtı sayılmaz.
- Scheduler rezervasyonu ve kullanıcı işi kontrolü sonrası tek koordineli gerçek
  AOS → Scientist → AOS GPU devri, sınırlı yerel model önerisi ve ölçümleri.

AOS oturumuna kısa aktarım: SQL0040 native CPU stop zinciri ve fiziksel cleanup
artık geçti; Scientist tek GPU tahsis otoritesidir. GPU testinden önce aynı wire1
sürümünde caller unit/principal/deployment/profile eşliği, cancel/status/reconcile
kontrol yüzeyi ve exact runtime drain/release kanıtı birlikte teyit edilmelidir.

## Açık arayüz ve deployment sınırı

Geliştirme kartı mevcut console overview polling'iyle beş saniyede bir okunur.
Bu koşuda gerçek ledger ilerlemesi yaklaşık dakikada bir, kritik aşamalar daha
sık public progress JSON'una atomik işlendi; son durum CPU Doğrulandı / GPU Engelli.
API/Director/AOS değiştirilmedi. **Ana runtime SQL0035 kaldı**; SQL0040 yalnız
izole kabul veritabanında kullanıldı. Console güncellemesi kullanıcının açık
arayüz talebiyle etkinleştirildi; deney çekirdeği deploy edilmedi.

Zorunlu kalite kapısı yedi exit0 / 1450 passed / 7 opt-in skipped / 120 GPU-live
deselected; TypeScript/Vite build exit0. M0 tam kabul sayısı artırılmadı:
11 passed / 7 partial / 4 open. Bu baseline/sentetik CPU kapanış kanıtı
bitmiş gerçek-model araştırması değildir. Lisans/genel CI ayrı tutulur;
push/merge/deploy yapılmadı (yalnız istenen yerel console yenilendi).
