# 75 — AOS GPU kontrol sözleşmesi önerisi

Sonraki kaynak uygulaması: [77 kontrol bağlantısı](77-aos-control-source-delivery.md).
Aşağıdaki kaynak hash'leri ve “uygulanmadı” ifadeleri önerinin ilk gözlemine
aittir. Scientist kaynaklarına artık bir implementation eklendi; ortak AOS
sözleşme teyidi, endpoint etkinleştirme ve gerçek GPU kabulü hâlâ açık.

30 Eylül 2026. **İnceleme önerisi; karşılıklı anlaşılmış, uygulanmış veya deploy
edilmiş değildir.** [74](74-aos-scientist-contract-response.md)'teki açık kontrol
transport'unu somutlaştırır. Endpoint'in etkin olduğu, capability verildiği veya
ortak GPU admission yapıldığı iddia edilmez. Scientist/root tek GPU kabul
yürütücüsüdür; canlı R10 kaynaklarına bu belge için dokunulmadı.

## Kaynak temeli ve mevcut sınır

Okuma anında Scientist HEAD `cc9511d4b449620b4bc1cc1a57ded41374eeb591`, AOS HEAD
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`. AOS'un gerçek değişmiş çalışma ağacı
ve `docs/SCIENTIST_HANDOFF.md` esas alınmıştır; eski patch snapshot'ı deployment
olarak kullanılmamıştır. Admission öncesinde iki çalışma ağacı, untracked runtime
kaynakları ve deployment fingerprint'leri yeniden doğrulanmalıdır.

| Mevcut kaynak / fonksiyon | Gözlenen davranış ve gereken ek |
| --- | --- |
| [aos_gpu_broker.py](../../lab/llm/aos_gpu_broker.py), `_decode_request`, `LabAOSBroker.serve_connection` | Infer tam altı alan, integer wire1, canonical JSON, tek newline, 128 KiB; SO_PEERCRED ve generation tekrar kontrolü. Dış kontrol operation yok. |
| [aos_gpu_service.py](../../lab/llm/aos_gpu_service.py), `_private_json`, `serve` | Private config, tek arbiter.sqlite3 ve dört uzun infer handler. Kontrol için ayrı bounded listener/capacity gerekir. |
| [aos_gpu_executor.py](../../lab/llm/aos_gpu_executor.py), `SystemdSocketPeerAuthenticator`, `BrokerOwnedTurnExecutor._intent`, `run_turn` | Tam PeerGeneration intent'te tutulur; aynı ID farklı generation/input ile reddedilir. Envelope deadline her çağrıda hesaplanır; yeni kontrol uygulaması özgün deadline'ı kalıcı tutmalıdır. |
| Aynı executor, `SystemdAOSProfileRuntime._launch`, `_wait_child_exit`, `verify_drained` | Exact child ve geç start fence'i, cgroup/GPU drain denetimleri var. Dış running-cancel sinyali ve kalıcı terminal kontrol receipt'i yok. |
| [gpu_scheduler.py](../../lab/llm/gpu_scheduler.py), `submit`, `try_acquire`, `cancel_queued`, `release`, `recover_quarantined`, `_finish` | Tek adil allocator. Cancel yalnız queued iç işlemdir. Trusted drain release önkoşuludur; `_finish` canlı owner/deadline alanlarını siler. Terminal kanıtı aynı transaction'da korunmalıdır. |

Bu dört dosyanın okuma anındaki SHA-256 değerleri sırasıyla:

```text
broker    3b34739ede0af5d14f4a3f57bfd254b20729885b431dbab71b1031060def33d9
service   9332db586f64316bfeb82e45de09b9a88f9f859f8141cdeee56444720a8ff245
executor  4247d676492ac7c57e6c2d864de444c87837c414a60383ced7e4b428f3a35f0b
scheduler 39fc9996d12be6b4616239086313ac0b460c10f7bf2f02ac4f31534e7aed22f8
```

[73](73-native-r9-terminal-stop-proof.md)'teki native stop/restart kanıtı
CPU/fake-json/sentetiktir. Aşağıdaki protokolü veya gerçek GPU iptalini doğrulamaz.

## Transport ve yetki

Infer sözleşmesi `aos-scientist-runtime.v1`, wire `1` olarak kalır: yalnız
`version`, `op`, `request_id`, `profile_id`, `deployment_digest`, `payload`.
Kontrol alanları infer'e eklenmez. Lab HTTP start/status/stop/report ayrıca kalır.

Önerilen ayrı şema adı **`aos-scientist-control.v1`**, integer kontrol sürümü
**`1`**. Ayrı Unix socket'in yolu private deployment config içinde
`control_socket` olarak iki tarafça pinlenir; bu belge aktif veya varsayılan yol
atamaz. Listener aynı Scientist servisine ve **aynı arbiter SQLite veritabanına**
bağlıdır; ikinci allocator, AOS'a DB erişimi veya caller release API'si kurulmaz.

Private policy varsayılan olarak deny'dır. Owner UID, izinli caller unit,
deployment digest, iki kaynak fingerprint'i, üç profile ait manifest/config ve
response-schema hash'leri, infer/control schema hash'leri ve izinli operation'lar
tam eşleşmeden capability verilmez. Symlink/foreign-owner/geniş izinli/unknown-key
config reddedilir. Socket parent private, socket 0600 ve mevcut canlı socket'i
değiştirmeme kuralı korunur. Aynı UID tek başına yetki sağlamaz.

Her bağlantıda `SO_PEERCRED` → `SystemdSocketPeerAuthenticator` ile **güncel tam
PeerGeneration** doğrulanır: `uid`, `pid`, `start_ticks`, `boot_id`, `unit`,
`invocation_id`, `control_group`, `parent_pid`, `parent_start_ticks`. Mutasyonun
transaction sınırında ve cevap öncesinde tekrar kontrol edilir. Client'ın verdiği
generation hash'i yalnız lookup beklentisidir; kimlik doğrulama kaynağı değildir.

Capability canonical belgesinin hash'i server generation, policy hash'i,
kaynak/deployment/profile/schema pinleri ve authenticated caller generation'a
bağlıdır. Bearer token değildir. Infer kabulü de sunucu tarafında aynı etkin
policy/pinleri denetler; altı alanlı wire nedeniyle istemcinin capability hash'i
infer'e taşınmaz. Policy/deployment değişiminde yeni admission kapanır; mevcut
işlerin yalnız özgün kayıtlarına bağlı cancel/reconcile ve iç cleanup yolu
korunur. Capability'nin model için uygun olduğunu doğrulamak AOS'un görevidir.

## Kesin istek ve cevap şekli

Canonical UTF-8 JSON, tek newline; duplicate/unknown alanlar, NaN/Infinity,
boolean-as-version ve trailing frame reddedilir. Önerilen kontrol sınırları:
istek 8 KiB, cevap 128 KiB, frame 5 saniye, tüm kontrol çağrısı 10 saniye.
İki ayrı kontrol worker'ı ve en çok sekiz bekleyen bağlantı; infer'in dört
worker'ı dolu olsa da kontrol kabul edilebilir. DB bekleme en çok 1 saniye;
systemd sorguları kalan çağrı bütçesiyle sınırlanır. Kontrol handler'ı drain,
model sonucu veya GPU lease beklemez. Kapasite yoksa bounded `busy`/bağlantı
kapanışı belirsizlik sayılır; tekrar yalnız kontrol operation'ına yapılır.

Her isteğin alan kümesi tam olarak şöyledir:

| Alan | Tip / kural |
| --- | --- |
| `schema`, `version` | Yukarıdaki kontrol şeması ve integer 1. |
| `op` | `capability`, `status`, `cancel`, `reconcile`. |
| `control_id` | 32 lowercase hex; bir kontrol isteğinin idempotency anahtarı. |
| `expected_capability_sha256` | Capability keşfinde null; diğerlerinde 64 lowercase hex. |
| `profile_id`, `deployment_digest` | Mevcut üç profilden biri ve 64 lowercase hex; capability dahil zorunlu. |
| `target` | Capability için null; diğerlerinde yalnız aşağıdaki üç alan. |

`target = {request_id, request_sha256, original_peer_generation_sha256}`.
Request ID 32 hex; iki hash 64 hex. `request_sha256`, **newline hariç özgün
canonical altı alanlı infer frame'inin** SHA-256 değeridir. AOS bu byte/hash ve
capability'nin verdiği generation hash'ini infer göndermeden journal'a kalıcı
yazar. Payload, lease/token, komut, dosya yolu, PID veya `drained` iddiası kontrol
isteğine alınmaz. `cancel` sebebi v1'de sabit `caller_cancel` olur.

Her cevap tam olarak `schema`, `version`, `control_id`, `op`, `ok`,
`capability_sha256`, `data`, `error` alanlarını içerir. Başarıda `error=null`;
hata halinde `data=null`, `error={code,retryable}`. Doğrulanamamış bağlantı
hiç receipt/kimlik vermeden kapatılabilir. Capability cevabındaki `data`:

- `capability`: server generation hash'i, authenticated caller tam generation
  ve hash'i, policy hash'i, Scientist/AOS source fingerprint'leri,
  deployment/profile manifest/config/response-schema hash'leri,
  infer/control schema adı/sürümü/hash'i, izinli operation'lar,
  frame/call sınırları, server `boot_id`, `issued_boottime`, `expires_boottime`.
- `admission`: `enabled` veya `denied`; deny halinde sabit `reason_code`.
  `enabled` süreli policy eşliğini bildirir, GPU tahsisi değildir. Önerilen
  capability ömrü 60 saniyedir; refresh queued/running deadline'ını uzatmaz.

Status/cancel/reconcile başarısında `data` tam olarak `target`, `state`,
`cancel_requested`, `first_cancel_boottime`, `terminal_receipt`, `result`
alanlarını içerir. İlk cancel zamanı yoksa null; result yalnız özgün generation
için tamamlanmış başarılı inference sonucudur, diğer durumlarda null.
`terminal_receipt` terminal kanıt yoksa null. Read operasyonları aynı DB
snapshot'ından cevap verir; `reconcile` iş başlatmaz/benimsemez.

| `state` | Anlam / AOS davranışı |
| --- | --- |
| `unknown` | Yetkili exact target için kayıt yok. Geç infer gelmeyeceğini kanıtlamaz; infer yeniden gönderilmez, belirsizlik fence'i korunur. |
| `intent`, `queued` | Admission henüz tamamlanmadı veya GPU bekliyor. |
| `activating`, `running`, `result_ready` | Tahsis aktif; sonuç hazır olsa bile fiziksel release kanıtlanmış değil. |
| `cancel_pending`, `draining` | Cancel ACK/durable intent var veya trusted cleanup ilerliyor; GPU kullanılabilir denmez. |
| `quarantined` | Kimlik/drain/timeout belirsizliği; fence korunur. |
| `completed`, `canceled`, `expired`, `failed` | Terminal yalnız aşağıdaki receipt ile. Release/no-admission kanıtı olmadan bu durumlar dışarı verilmez. |

`error.code` kapalı enum'u: `invalid_frame`, `unsupported_version`,
`unsupported_schema`, `unauthorized`, `stale_generation`, `capability_mismatch`,
`capability_expired`, `profile_mismatch`, `deployment_mismatch`,
`request_conflict`, `history_denied`, `busy`, `deadline_exceeded`,
`internal_unavailable`. Yalnız son üçü kontrolü yeniden denemeye uygundur;
capability hatasında önce yeniden capability alınır. `unknown` hata değildir.
Foreign target varlığını veya output'unu hata mesajı açığa çıkarmaz.
Timeout/disconnect başarı veya iptal anlamına gelmez; yeni kontrol ID ile
status/reconcile yapılabilir. Hiçbir kontrol timeout'u özgün işi uzatmaz.

## İdempotency, cancel yarışı ve terminal kayıt

`control_id` + authenticated generation için canonical istek hash'i tutulur;
aynı ID farklı içerik `request_conflict` olur. Tekrarlı cancel ilk intent/zamanı
korur, güncel state'i okuyabilir. Status snapshot'ı tekrar okumada ilerleyebilir.
İş kimliği her zaman özgün request hash'i, **tam özgün PeerGeneration**, profile
ve deployment/config pinleriyle korunur; unbound legacy kayıt benimsenmez.
Özgün admission zamanı, boot kimliği, envelope/queue/activation/inference/total
deadline'ları ve token bütçeleri kalıcıdır. Reconnect/restart/reconcile/cancel
yeni süre, token veya GPU tahsisi vermez; boot değişiminde süre tahmin edilmez.

1. **Cancel intent'ten önce gelirse:** aynı arbiter DB'de `BEGIN IMMEDIATE`
   altında exact target + authenticated generation için `cancel_before_intent`
   tombstone yazılır. Infer `_intent` ve scheduler submit/acquire yolu aynı
   transaction düzeninde tombstone'u denetler. Tombstone commit'inden sonra geç
   infer admit edilemez. Yalnız `unknown` okumak bu güvenceyi vermez.
2. **Queued cancel:** tombstone/cancel intent ile queued→canceled ve
   no-allocation terminal kaydı tek transaction'da olur. Acquire yarışı önce
   kazanmışsa işlem running-cancel yoluna geçer; yanlış no-allocation receipt
   üretilemez. `_intent`→submit ve acquire→child plan aralıkları da korunur.
3. **Activating/running cancel:** transaction yalnız kalıcı cancel intent ve
   exact allocation bağını yazar. ACK lease'i bırakmaz. Executor launch/go
   öncesinde ve bounded bekleme döngüsünde intent'i denetler; broker restart'ında
   trusted recovery aynı kaydı işler. Geç systemd start olasılığı child intent
   fence'iyle kapsanır. Cancellation ve child plan/go handoff'u aynı kilit/CAS
   düzeniyle sıralanır; yalnız launch öncesi bir kez flag okumak yeterli değildir.
4. **Fiziksel cleanup:** trusted worker exact child unit/invocation/PID/start
   ticks/boot/cgroup ve gözlenen GPU PID'leriyle stop/drain yapar. İç
   `release`/`recover_quarantined` dışında lease kaldırılmaz. Başarısız/gözlenemeyen
   drain quarantine bırakır; caller'ın ACK, idle veya worker-exit iddiası yetmez.
5. **Sonuç/cancel yarışı:** completed receipt önce commit olmuşsa cancel mevcut
   completed'i döndürür. Cancel intent önce commit olmuşsa başarı yayımlanmaz;
   cleanup ardından canceled receipt oluşur. Result-ready tek başına terminal
   değildir. Kesin sıralama aynı arbiter transaction'larıyla kanıtlanır.

Terminal receipt şeması `aos-scientist-terminal.v1` önerilir. Tam alanları:
`schema`, `request_id`, `request_sha256`, `original_principal`, `profile_id`,
`deployment_digest`, `profile_config_sha256`, `response_schema_sha256`,
`original_budget`, `allocation_binding_sha256`, `child_generation`,
`drain_evidence_sha256`, `no_admission_evidence_sha256`, `release_outcome`,
`terminal_state`, `reason_code`,
`result_sha256`, `recorded_boot_id`, `recorded_boottime`, `receipt_sha256`.
`original_principal` tam özgün PeerGeneration'dır. `original_budget` yukarıdaki
ilk zaman/deadline/token değerleridir. `child_generation` gerçek gözlenmiş
unit/invocation/PID/start_ticks/boot/cgroup'dur; hiç child yoksa null.

İç immutable allocation kaydı owner/request/original principal, scheduler
fencing token, özgün lease/deadline ve child intent bağlarını içerir;
`allocation_binding_sha256` bu kaydın hash'idir. Caller fencing token/lease
seçmez veya release isteğinde sunmaz. Fiziksel kanıt kaydı gözlemler, exact
child, late-start fence, cgroup emptiness ve GPU absence sonucuna bağlıdır.
`release_outcome` yalnız `never_admitted`, `released`, `recovered_released`;
son ikisi gerçek trusted drain kanıtı gerektirir. Never-admitted halinde
allocation/child/drain alanları null ve kalıcı no-admission/tombstone kanıtı
`no_admission_evidence_sha256` ile receipt'e bağlıdır; diğer release outcome'larda
bu alan null'dır. Başarı dışındaki `result_sha256` null'dır. Terminal
`reason_code` enum'u `success`, `caller_cancel`, `queue_timeout`, `turn_timeout`,
`generation_lost`, `execution_failed`, `recovered_after_crash` olur. Mevcut
deadline/ölçülmüş kullanım ayrı tutulur; token bütçesi ölçülmüş token tüketimi
olarak sunulmaz.

`receipt_sha256` kendi alanı hariç canonical receipt'in hash'idir; tek başına
imza/yetki kanıtı değildir. Receipt authenticated socket'ten okunur, iç kanıtlar
Scientist'te saklanır. `_finish` canlı alanları silmeden önce immutable lease,
owner, deadline, child/drain ve release sonucunu **aynı transaction'da** terminal
kayda bağlar. Önceden yazılmış fiziksel kanıt tek başına release sayılmaz;
son CAS başarısızsa terminal receipt yoktur. `_finish` commit'i ile cevap/cleanup
arasındaki crash'te reconcile aynı receipt'i verir. Kayıt/tombstone silinerek
request ID yeniden açılmaz; retention/disk sınırı dolarsa yeni admission kapanır.

## Caller restart ve karşılıklı kararlar

Yeni caller invocation eski generation'ın işini adopt, cancel, resume veya
yeniden infer edemez. Varsayılan policy history erişimini reddeder. İki taraf
isterse ayrı **history ACL**, aynı configured unit + aynı deployment + tam
target için yalnız `reconcile` izni verir. Güncel caller yine SO_PEERCRED ile
doğrulanır; eski principal değiştirilmez. Bu cevap sadece target/state,
original-generation hash'i, terminal receipt hash'i ve release outcome içeren
redacted projection'dır; model output/payload, tam process kimliği ve özel yollar
verilmez. Bu şema ayrı hash ile capability'de ilan edilir. İç recovery eski
fence'i çözer; history izni kendisi GPU mülkiyeti veya mutasyon yetkisi vermez.

AOS ile teyit edilmesi gereken somut kararlar:

1. Ayrı control schema/socket ve exact frame/operation alanları kabul ediliyor mu;
   deployment config'teki socket sahibi/yolu ile gerçek caller unit/principal
   hangi değerlerle pinlenecek?
2. Canonical infer byte/hash + original-generation hash journal'a **göndermeden
   önce** yazılacak mı; `unknown`, lost ACK ve timeout halinde infer tekrarının
   kapalı kalması ve cancel-before-intent receipt'i kabul ediliyor mu?
3. Source/deployment/profile/schema hash sağlayıcısı ve 60 saniyelik capability
   yenilemesi nerede doğrulanacak; mismatch'te default-denied kalacak mı?
4. Cancel ACK'nin release olmadığı, terminal receipt'in tek reopen kanıtı olduğu
   ve queued/running yarış sırası kabul ediliyor mu? Yeni invocation için history
   ACL kapalı mı kalacak, yoksa yukarıdaki redacted reconcile açılacak mı?
5. Önerilen kontrol kapasitesi/süre sınırları ve disk dolduğunda admission deny
   davranışı kabul ediliyor mu; iki tarafın sınırlı gerçek GPU kabul oturumu için
   source/deployment pair'i ve ölçüm planı birlikte ne zaman dondurulacak?

## Anlaşmadan sonra uygulama sırası ve kanıt

Önce iki tarafta şema/hash ve private policy fixture'ları incelenir. Ardından
Scientist'in aynı arbiter DB'sinde additive kontrol intent/tombstone/immutable
receipt değişiklikleri; submit/acquire/launch ve `_finish` transaction bağları
uygulanır. Ayrı listener, capability/status/cancel/reconcile ve trusted runtime
cancel gözlemi eklenir. AOS typed client/journal bağları koordineli kendi çalışma
alanında hazırlanır; hazır olmayan capability provider deny kalır.

CPU doğrulama; dört infer handler doluyken kontrol yanıtı, stale/foreign principal,
hash/version/profile mismatch, duplicate ID conflict, cancel-before-intent,
queued↔acquire, acquire↔launch, ready↔cancel, finish↔crash/lost ACK, broker/caller
restart, false drain ve history ACL durumlarını kapsamalıdır. Test double kanıtı
CPU olarak etiketlenir. Sonrasında root'un tek koordine opt-in oturumunda gerçek
AOS → Scientist → AOS ilerlemesi, bounded model çağrısı, queued/running cancel,
lost ACK reconcile, exact fiziksel drain/release, VRAM/latency ve aggregate
CPU/RAM/disk ölçülür. Bu belge hiçbir M0/AOS kabulünü kapatmaz.
