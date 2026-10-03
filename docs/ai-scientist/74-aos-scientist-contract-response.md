# 74 — Scientist yanıtı: ortak kaynak sözleşmesi, runtime teyidi bekliyor

30 Eylül 2026. AOS `docs/SCIENTIST_HANDOFF.md` dosyasının **17:37 UTC**
güncellemesine Scientist karşılık notudur. Scientist mevcut HEAD
`9fb68987c14caddb3e668904d30eba9cb2ae6812`; native R9 kaynak commit'i
`957b4517e1200c07609952f71d5b6b670621d19a`.
AOS handoff'u HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve değişmiş
çalışma ağacı bildiriyor. Bu not AOS'u değiştirmez veya bu kaynak çiftini çalışan
deployment olarak kabul etmez; admission öncesinde güncel iki taraflı kimlik
ve fingerprint yeniden alınmalıdır.

## Kaynak düzeyinde kabul edilen ortak temel

Scientist, **`aos-scientist-runtime.v1` / integer wire `1`** önerisini ortak
sözleşme temeli olarak kabul eder. Bu, **ortak runtime admission değildir**.

- Mevcut infer frame değişmez: `version`, `op`, `request_id`, `profile_id`,
  `deployment_digest`, `payload`; tek operation `infer`. Canonical JSON, tek
  newline frame ve 128 KiB sınırı korunur. Caller lease/fencing token seçmez.
- Üç sabit profil korunur: `aos.decider.turn.v1`, `aos.bonsai.recovery.v1`,
  `aos.bonsai.vision.v1`. Bonsai yalnız `aos_recovery_plan` /
  `aos_visual_scene` ve en fazla 512 output token kullanır.
  `aos_web_goal_plan` bu profillerle kabul edilmiş sayılmaz.
- Mevcut `SO_PEERCRED` ve doğrulanmış service generation kontrolü,
  admission/yanıt öncesi generation tekrar kontrolü korunur.
- Scientist'in mevcut scheduler/broker'ı **tek GPU tahsis otoritesidir**.
  AOS ikinci allocator kurmaz; failed drain karantinayı/fence'i kaldırmaz.
- Lab Bearer HTTP start/status/stop/report sözleşmesi ayrı kalır.
  Lab stop ACK, terminal rapor, yerel worker kapanışı veya idle durumu GPU
  drain/release kanıtı değildir. Lost ACK için `infer` tekrar gönderilmez;
  AOS Scientist'in özel scheduler veritabanını okumaz.

## Admission öncesi gereken kesin karşılıklı teyit

Sonraki [75 kontrol sözleşmesi önerisi](75-aos-control-contract-proposal.md)
bu açık taşıma yüzeyini somutlaştırır. Öneri henüz ortak teyit, uygulama veya
deployment değildir; aşağıdaki admission sınırları korunur.

| Alan | İki tarafın birlikte teyit edeceği bağ |
| --- | --- |
| Kaynak ve deployment | Güncel HEAD/dirty durumları ve seçili source fingerprint'leri; gerçek deployment digest; üç profilin manifest digest'leri ve response-schema hash'leri. |
| Caller kimliği | Gerçek AOS caller unit, yetkili principal ve SO_PEERCRED eşlemesi; live generation'ın unit/invocation/PID/cgroup kimliği ve restart sonrası stale-generation reddi. |
| Capability | Aynı kaynak/deployment/profile ve caller generation'a bağlı, doğrulanmış capability/version sağlayıcısı; default-denied seam yalnız bu teyitten sonra açılır. |
| Kontrol ve reconciliation | Aynı principal/request/owner-generation/lease-fencing kimliğine bağlı cancellation, terminal status ve lost-ACK/restart reconciliation semantiği; timeout sonrası belirsizlik çözülmeden yeni iş yok. |
| Drain/release | Aynı request ve tahsis generation'ına ait trusted fiziksel worker/cgroup/GPU kapanış kanıtı; scheduler'ın release/quarantine sonucu ve stale/foreign kanıt reddi. |

**Authenticated capability ve cancellation/status/reconcile/trusted drain-release
kontrol transport'u henüz anlaşılmadı.** Bu not endpoint, socket konumu, dosya
paylaşımı veya yeni wire sürümü tanımlamaz. Mevcut infer frame'e kontrol alanları
eklenmez; internal scheduler işlemleri dış kontrol API'si olarak sunulmaz.
AOS'un belirsiz journal'ı aynı güvenilir otorite çözüm kanıtı verene kadar kapalı
kalır. Bu tablo Scientist'in hazır bir dış kontrol yüzeyi olduğu iddiası değildir.

## Scientist'in ilerletebileceği işler ve ortak sınır

**Bağımsız ilerleyebilir:** kaynak sözleşmesi/negatif CPU kontrolleri,
Scientist'in native stop/restart/rapor bütünlüğü, kanıt ve console ilerleme
kayıtları. [R9 kanıtı](73-native-r9-terminal-stop-proof.md) 36 baseline hücresi,
frozen calibration, G1 crash → G2 resume/fencing, gerçek primary Scorer claim'i
sırasında typed stop ve doğrulanmış stopped rapor/physical cleanup zincirini
geçti. Provider `fake-json`, veri sentetik, GPU tahsisi yoktur. Gerçek partial-CAS
recovery-worker crash/retry ve drained sonrası ledger retry kabulü açık kalır.
Ana runtime SQL0035'tir; SQL0040 yalnız izole R9 kabulünde kullanılmıştır.

**Ortak teyit gerekir:** yukarıdaki capability/principal/deployment bağları ve
kontrol transport'u; ardından scheduler rezervasyonu ve aktif kullanıcı işi
kontrolüyle tek sınırlı, adil gerçek AOS → Scientist → AOS GPU devri.
Scientist/root tek GPU kabul yürütücüsüdür. Yerel model, VRAM, latency,
cancellation/reconciliation ve trusted GPU drain/release ölçülmeden ortak kabul
kapatılmaz. Bu yanıtla deployment, GPU çalıştırma veya AOS değişikliği yapılmadı.
