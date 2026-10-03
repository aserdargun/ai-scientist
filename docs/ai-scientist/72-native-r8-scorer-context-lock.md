# Native R8: Scorer recovery kilidi ve korunan quarantine

Scientist başlangıç commit'i `1b069ea40c6db9dea4b6d0b4c2e6a221478aa074`;
AOS salt okunur HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
İzole R8 run `1135afc0-9f07-419e-8d9a-a67dff3e4133`, SQL0039.
Frozen kaynak manifest SHA-256
`2163f6ac1e15fa2e7de6742508e387582a01f5dd5ddc8505083fa97cded981fd`.
2400 saniye / 1 deney / 24576 token rezervasyonu; dört sentetik aile,
256 train / 384 eval, fake-json provider. Ürün runtime'ı deploy edilmedi.

## Geçen

- 36 gerçek bağımsız baseline hücresi, üç scored baseline ve frozen calibration.
- Exact G1 kontrollü crash, G2 resume, gecikmiş G1 retleri ve özgün deadline.
- Gerçek primary Scorer claim running iken typed stop; tekrarlı stop ilk zamanı korudu.
- V2 stop marker ve pending closure normal uygulama yoluyla yaratıldı.
- Bağımsız fiziksel kontrol exit0: G1/G2 ölü, cgroup'ları boş, tüm 37 Scorer
  işçisi quiescent. API/parent inactive ve MainPID0; özgün kayıtlar korunuyor.

## Başarısız kapanış

Koşu exit1 / 1285.848695 saniye; kaynak hash'leri değişmedi.
Recovery actor kaydı0, child/final seal NULL, terminal rapor NULL;
ledger stop_requested ve primary running job1 quarantine'da kaldı.
Stop ACK veya fiziksel quiescence terminal durum olarak sayılmadı.

PG context, `assert_stop_v2_context` → `lock_run_plan` çağrısında
Scorer rolünün reddedildiğini gösterdi. Normal Scorer DSN ile aynı gerçek
recovery receipt çağrısı, hiçbir claim GUC veya veri yazımı olmadan
`P0001` hatasını yeniden üretti. İnceleme `reconcile_stop_job_v37` fallback'inde
aynı uyumsuz kilit çağrısını da buldu.

## Dar SQL0040 düzeltmesi

İki private guard içinde Scorer aynı SHA256(run UUID) transaction advisory
lock'unu alır. Director/Planner mevcut `lock_run_plan` ve captured owner-run
kontrolünü kullanmaya devam eder. Genel rol/ACL veya GPU tahsis yetkisi
artırılmaz. Owner/generation/execution, atomik satır kilitleri, özgün stop
penceresi ve immutable plan/roster/score kontrolleri korunur.
Eski migration'lar değiştirilmez. Süresi dolmuş R8 kapanışını geçirmek için
hiçbir deadline, durum veya owner değiştirilmez.

Normal Migrator ile yalnız bu oturumun R8 veritabanına SQL0040 uygulandı;
run/contract/job/score/recovery/closure/marker/actor snapshot'ları önce ve sonra
aynı kaldı. Aynı gerçek Scorer receipt artık kilit kontrolünü geçti ve özgün
`attempted stop original deadline expired` güvenlik kontrolünde reddedildi.
Transaction rollback edildi. Bu kanıt rol hatasının giderilmesi ve expiry
korunmasıdır; terminal stop kabulü değildir. Migration SHA-256
`9f79f593af724cdf033417aeeb515a55779031f6df50799cfb8dc2cf278ae033`.
Kritik kaynak incelemesi iki çağrı yolunu ve sonraki v2 producer rollerini
doğruladı; ilave bir deterministik rol uyuşmazlığı bulunmadı.


## Ölçüm ve kapsam sınırı

Başlangıç 16:33:11.732685 UTC; calibration 16:53:17.320993 UTC
(1205.588308 saniye). İlk stop 16:53:53.707456 UTC;
120 saniyelik özgün stop penceresi 16:55:53.707456 UTC'de doldu.
GPU acquire/wait/devir/drain gecikmeleri ve VRAM tepesi ölçülmedi.
Model/quantization/context: gerçek LLM yok; fake-json fixture, quantization
uygulanmaz, gerçek model context ayarı ve ölçülmüş token kullanımı yok.

## Kalan / çalıştırılmayan

- SQL0040 ile yeni native koşuda terminal stopped rapor, üç dürüst eksik-cell
  completion, recovery actor retirement ve final seal.
- Partial-CAS recovery worker crash/retry ve drained proof sonrası ledger retry.
- AOS ortak capability/version/principal, cancel/reconcile ve trusted drain/release
  teyidi; rezervasyon sonrası tek koordineli gerçek GPU kabulü.

AOS incelemesinde kaynak hash'leri aynı kaldı; güncellenen handoff belgesi ortak
runtime kabulünü hâlâ bekliyor. Aktarılacak kısa sözleşme özeti:
[aos-scientist-runtime.v1 / wire1 beklentileri](70-aos-runtime-readiness.md).
Scientist tek GPU otoritesidir; AOS idle veya stop ACK release kanıtı değildir.
M0 toplamı artırılmadı. Lisans/genel CI ayrı; push/merge/deploy yapılmadı.

## Kalite kapısı ve görünen ilerleme

SQL0040'ın altı regresyonu dahil zorunlu yedi komut exit0; **1450 geçti /
7 opt-in atlandı / 120 GPU/live seçilmedi**. Native runtime kaynağı koşu boyunca
değişmedi. Arayüz TypeScript/Vite derlemesi exit0. Yalnız bu projenin console
servisi kimliği doğrulanıp yenilendi; API/Director invocation'ları aynı kaldı.
Canlı endpoint güncel JSON'u, sunulan frontend yeni ilerleme kartını içeriyor.
Kart mevcut beş saniyelik polling ile güncellenir; on dakikadan eski kayıt
bekleyen güncelleme olarak gösterilir. M0 sayımları kanıt dosyasından gelir.

Sonraki yeni SQL0040 koşusunda terminal zincir ve cleanup geçti:
[Native R9 gerçek CPU kanıtı](73-native-r9-terminal-stop-proof.md).
R8 quarantine kayıtları bu yeni kabul ile değiştirilmedi.
