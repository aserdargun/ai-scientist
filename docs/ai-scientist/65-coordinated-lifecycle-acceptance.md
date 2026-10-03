# Güvenli stop ve koordineli AOS kabulü

2026-09-30. Bu çalışma mevcut M0/OM kabullerine ek önceliktir;
tamamlanmış araştırma veya gerçek GPU kabulü değildir. Yalnız Scientist
checkout'u değiştirilebilir. AOS salt okunur; push, merge ve deploy yok.
GPU kabulünün tek yürütücüsü Scientist ana oturumudur.

## Doğrulanan başlangıç

- Scientist yerel HEAD: `14a2c83fb579e4f658db836cbab8f3e8803bc07f`;
  public inceleme referansı `67258cdef33032c9a49eeae31c2e2ba26a98ca17`.
  Başlangıçta staged/unstaged fark boş; ikisinin SHA256'sı
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- Gerçek AOS HEAD: `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`,
  verilen `22e5736` referansından sonraki kaynak esas alınır. Tracked fark
  boş; yalnız mevcut untracked `AI_SCIENTIST_COORDINATION.md` korunur.
- Ana Director PID `1046541`, invocation
  `d2c61d7fb7a24479af69d9db0814dcf5`; API PID `801012`, invocation
  `702ca80434344459a90bb0d8292433d5`. Salt okunur PostgreSQL kontrolünde
  queued=0, current-generation active=0, active score jobs=0.
  İki eski ownerless koşu (`75642033…` stop_requested, `b54c9282…` running)
  korunur; bunlar durmuş veya cleanup tamamlanmış sayılmaz.
- GPU anlık toplam 62 MiB / 16376 MiB, utilization %0; listede yalnız
  KWin PID1368, 12 MiB. Bu anlık gözlem release veya rezervasyon kanıtı
  değildir. Sabit ortak `~/.local/state/swapp-gpu/arbiter.sqlite3` mevcut
  değil; bu inceleme scheduler yaratmadı veya tahsis almadı.

## Geçen CPU kontrolleri ve açık stop hatası

Gerçek R3 inflight stop sonrasında running-only failure-close RPC reddi
korunan başarısızlık kanıtıdır. Aynı hatayı yakalayan üç regresyon, ana
runtime üzerinde tekrar **3 failed / exit1** verdi. R5 adayında owner,
generation, original deadline, callback sonrası drain retirement,
Scorer kimliği ve GPU quarantine testleri **67 passed / exit0** verdi.
Özel log SHA256'ları:

- Önce: `4fc95a253c47a939687b9cf179aa4860dfafd6153b93ac711b07b815391c46b2`.
- Aday: `b2944db2ae06ee1fb32db854f00f5279f9951e7d8a1b08c3e772c4aeb9a85ff7`.

R4 gerçek inflight denemesinde own exit75 ve successor restart gerçekleşti;
terminal rapor oluşmadı. İkinci hata: reconciliation producer failed job
yazarken claimed_by/lease_until bırakıyor; mevcut0009 invocation guard
haklı olarak reddediyor. R5 STOP0036 adayının kapsamı yalnız bu iki alanı
temizlemek ve mevcut stopped-transition guard içinde aynı sınırlı deltayı
kabul etmek. Immutable original_job, claim unit/invocation, exact owner,
generation, row CAS, rol ve ilk stop deadline korunmalı. Statik SQL testi
native PostgreSQL transaction kanıtı değildir. R5 üretime uygulanmadı.

Ana kaynağa iki GPU regresyonu eklendi: başarısız cleanup release/devri
engeller; eski/tekrarlı release yeni tahsisi kapatamaz. Scheduler suite
**29 passed / exit0**, bounded CPU unit peak74.3 MiB, swap0. Mevcut SQLite
scheduler ve trusted drain verifier değiştirilmedi. Tekrarlı stale release
anlaşılır conflict ile reddedilir; başka tahsise etkisi olmaz.

## AOS oturumuna aktarılacak kısa sözleşme özeti

İlk incelemede `ed6e857` checkout'unda `services/decider/broker_worker.py`,
`services/bonsai/broker_worker.py`, `shared-gpu-turns` ve `lab-external`
seçenekleri bulunmuyor. Tarihsel izole yamalı kopya gerçek checkout değildir.
Sonraki paralel kaynak çalışması iki worker dosyasını ekledi; bunlar hâlâ
untracked ve ortak runtime admission tamamlanmış değil. Güncel inceleme ve
ayrı source profilleri [67 numaralı kayıttadır](67-native-calibration-stop-and-aos-preflight.md).
Scientist'ta fixed-frame broker `version=1`, `op=infer` mevcut; bu tek başına
iki tarafın uzlaştığı capability/version sözleşmesi değildir. Ortak öneri
ve teyit beklenir; uyumsuz checkout gerçek koşuya alınmaz.

Salt okunur ön kontrol:

```bash
.venv/bin/python scripts/check_aos_lab_compatibility.py \
  --aos-root /home/cachyos/aos \
  --expected-head ed6e857b0e61e9c19c8ba63933e2cc9f318fe444 \
  --source-profile runtime_v1
```

Gerçek checkout sonucu exit2/unsupported; deney admission izni false.
Kaynak işaretleri uyumlu olsa bile ortak runtime capability teyidi olmadan
exit3/pending verir. Bu script tahsis otoritesi değildir. Sekiz yeni CPU
testi eksik/uyumsuz sürüm, yanlış checkout, dirty source ve sahte kaynak
işaretlerinin admission sağlayamamasını denetler.

İnceleme sırasında AOS'un `reusable_decider.py` ve testinde paralel tracked
değişiklikler oluştu; dokunulmadı. Son gözlenen tracked fark SHA256:
`1a171c1501e9f121546140e0845e0c5567baf64a8bff00258c606de9c1ca52f9`.
Hash bir anlık kayıttır; sonraki kabulde yeniden kontrol edilir.

Ana kaynak kalite kapısı R3: **1257 passed / 7 skipped / 120 deselected**,
yedi gerçek exit0, peak589.6 MiB/swap0. İlk koşunun uzun TMPDIR ve eksik
uv PATH hataları korunur; R2 yalnız bize ait gate unit'i sonlandırılarak
konfigürasyon düzeltildi. R3 kısa owned TMPDIR ve explicit PATH kullandı.
[Kaynak ve icra özeti](review-evidence/coordinated-lifecycle-20260930-summary.json).

Lütfen adapter sözleşmesiyle birlikte şu bilgileri aktarın:

1. AOS commit/fark hash'i, capability/version cevabı ve opt-in CLI seçenekleri.
2. Tek mevcut scheduler'ın yolu ve owner/principal doğrulaması; request digest,
   run/job/action kimlikleri, fencing token ve generation eşlemesi.
3. Acquire/timeout/revoke/cancel ve drain/release sırası. Stop ACK veya AOS
   idle/quiesce release kanıtı değildir. Exact child invocation/cgroup/PID
   ve GPU unload kanıtı yoksa quarantine korunmalı; eski yanıt yeni işi
   kapatmamalı. Retry aynı immutable key/digest/deadline kullanmalı.
4. Ayrı opt-in instance için güvenli rezervasyon ve kullanıcı işi bulunmadığı
   teyidi. GPU kabul koşusunu yalnız Scientist ana oturumu başlatacak.
5. Typed start/status/stop/report ve canonical terminal report hash doğrulaması;
   uzun Lab işi AOS foreground slotunu tutmamalı.

## Kalan ve çalıştırılmayan kabul

### Gerçek inflight stop ve principal replay düzeltmesi

Son kaynak entegrasyonu: test edilmiş R5 CLI/recovery/stop_closure ve0036
migration artık ana çalışma ağacında, iki regresyon dosyasıyla birlikte.
Çalışan üretim servisleri/schema0035 değiştirilmedi; bu kaynak teslimi
deploy değildir. Direct yolun18 kontrollü gerçek kanıtı korunur.
Shared-drain exit75 sonrası successor için Restart=on-failure/5s gerekir;
mevcut üretim Restart=no korunur. Startup değişikliği yalnız
[inceleme yaması](review-evidence/automatic-stop036-startup-policy.review.patch)
olarak tutulur; git apply --check geçti, uygulanmadı. Çalışan sistemin
otomatik stop özelliği etkinleştirilmiş sayılmaz.

Ek protokol testleri **24 passed/exit0**: gerçek AF_UNIX/SO_PEERCRED,
fixture auth/executor ile revoke, generation değişimi, malformed frame,
UTF-8 canonical digest ve128KiB sınırları; mevcut SQLite scheduler'da
queued cancellation idempotency ve active lease'in queued-cancel ile
bırakılamaması. Bunlar fiziksel systemd principal/GPU kabulü değildir.

Ayrı ownPG55545'te Director rolünün stopped-job RPC'si gerçek SQLSTATE42501
ile reddedildi; score job değişmedi. Bu terminal fixture üzerinde rol/GRANT
fence kanıtıdır; pending-row CAS/generation/deadline negatiflerini kanıtlamaz.
[Dar native rol kanıtı](review-evidence/native-stop036-wrong-role.json).

AOS'un güncel çalışma ağacında ayrıca default-denied
`ScientistTurnClient` transport kütüphanesi var. Socket kapatmak Scientist
ticket'ını iptal etmez. Capability, durable task/intent adapter ve kontrol
yüzeyi tamamlanmadan gerçek admission hâlâ kapalıdır. AOS kaynakları ve
testleri bu oturumdan çalıştırılmadı veya değiştirilmedi.

Yeni ayrı PostgreSQL55545 ortamı normal migration ile0036'ya yükseltildi;
boş ledger ve dört exact-role kontrolü exit0. Normal fixture kurulumuyla
dört bağımsız sentetik aile eklendi. Üretim Director'ünün lock dosyasıyla
aynı dev/inode'a bağlı P1 lock kullanıldı; ikinci admission kilidi yok.

İlk observer isteği zorunlu AOS task/run/action kimlikleri olmadığından
HTTP422 ile reddedildi; own ledger'da **sıfır run** doğrulandı. Bu başarısız
kayıt korunur. Yeni observer yalnız bu typed kimlikleri ekledi; guard
gevşetmedi. Kimlikler test tarafından üretildi, gerçek AOS işi değildir.

Yeni `389ebd93-aabf-4adb-a417-c5abece9ea0f` koşusunda **18 kontrol/exit0**:
gerçek Scorer inflight claim → typed idempotent stop → direct owner exit →
normal automatic recovery → `stopped` ve bağımsız doğrulanmış rapor hash'i.
Original owner/generation/deadline korundu; claimed_by/lease_until temizlendi,
original_job ve claim unit/invocation korundu. İlk Scorer PID/cgroup kapandı,
aktif score job sıfır ve 12 girdilik terminal task planı mühürlendi.
Üretim API/console/Director kimlikleri değişmedi; yalnız exact owned test API
kapatıldı. Yeni bounded PostgreSQL follow-up için korundu.
[Native direct kanıtı](review-evidence/automatic-stop036-native-direct-proof.json).

Bu kanıt fake proposal provider kullanan sentetik CPU lifecycle testidir;
araştırma veya GPU kabulü değildir. R5 adayı üretime uygulanmadı. Native
wrong-role/stale-CAS/actual-control-generation/expired-deadline negatif
koşuları ve shared-drain successor yolunun tam kabulü ayrıca açık.

Broker'da ayrı bir CPU regresyonu doğrulandı: yeni authenticated AOS
generation'ı eski tamamlanmış sonucu replay edebiliyordu. Önce exit1,
2 failed/1 passed; dar düzeltme sonrası **42 CPU testi/exit0**. Intent artık
tam özgün PeerGeneration'a bağlı; legacy unbound kayıtlar sahiplenilemez.
Mevcut scheduler/bütçeler/quarantine ve result_ready→completed davranışı
korundu. Kaynak düzeltmesi var; çalışan broker veya ortak SQLite değiştirilmedi.

Önemli sözleşme ayrımı: immutable request/profile/deployment/config digest
ve durable result replay **zaten mevcut**. Eksik olan bunlara ait dış
capability/status/reconcile/cancel kontrol yüzeyidir. Mevcut UDS yalnız
`infer` kabul eder; bağlantı kopması aktif inference'ı anında iptal etmez.
İç `cancel_queued` yalnız authenticated queued ticket içindir. Aktif hata,
timeout ve cleanup yolları trusted drain üzerinden release dener; başarısız
drain quarantine/fencing'i bırakmaz. Kamuya açık cancel API varmış gibi
sunulmaz; yeni endpoint üzerinde henüz ortak karar yok.

### Yeni AOS önerisinin salt okunur incelemesi

İlk kayıt ve yerel Scientist commit'inden sonra AOS oturumu
`docs/SCIENTIST_RUNTIME_INTEGRATION.md`, `src/aos/scientist_protocol.py`
ve dört schema dosyasını untracked olarak hazırladı. Bu daha yeni çalışma
korunur; committed runtime/admission capability diye sunulmaz.

Öneri **`aos-scientist-runtime.v1`**: wire1, üç mevcut fixed profile,
128KiB newline frame, UTF-8 canonical digest ve Scientist'ın tek tahsis
otoritesi korunuyor. Bu protokol yönü Scientist tarafında uygundur;
uygulanan ortak runtime sözleşmesi henüz teyit edilmedi. `purpose`
alanının baseline/research/mode-grid veya null olması mevcut API ile
uyumludur. Receipt generation unit/invocation/PID/cgroup alanları mevcut
broker çıktısıyla eşleşir; caller-supplied receipt tek başına cleanup
attestation değildir. Yeni AOS kaynağı inceleme sırasında değiştiğinden
kabul öncesinde final kaynak hash'leri yeniden sabitlenmelidir.

AOS'a yanıt: capability endpoint'i veya taşıması henüz uzlaşılmadı; mevcut
broker sabit infer frame'ini destekler. Bu frame'e lease/cancel alanları
eklemeyelim. Durable uncertain-request reconciliation ve cancellation
kontrol yüzeyi açık iş olarak kalır; kayıp ACK sonrasında otomatik yeni
request/key üretilmez. Owner/generation/fencing scheduler'da doğrulanır.
Terminal report hash bağımsız authenticated status/readback ile doğrulanır;
GPU cleanup için trusted exact invocation/cgroup/GPU absence ayrıca gerekir.
Scientist yalnız bu koşullar ve rezervasyon doğrulandığında tek GPU kabul
yürütücüsü olacak. Üretim Director restart/swap yapmadan ayrı canonical
runUUID dispatch birimiyle native stop kanıtı hazırlığı devam ediyor.

Kalan: native PostgreSQL negatifleri ve idempotent RPC retry/stale generation
testleri; shared-drain successor yolunun tam kabulü; uyumlu AOS sözleşmesi
ve admission capability kontrolü. Direct inflight pozitif yol yukarıda
kanıtlandı; bu diğer açık maddeleri kapatmaz.
Trigger kapatma veya session_replication_role bypass ile kanıt üretilmez.

Çalıştırılmayan: AOS→Scientist öneri/bağımsız puanlama→AOS rapor doğrulama
GPU devri; kontrollü gerçek iptal/toparlanma. Model/quantization/context,
VRAM tepe ve queue/handoff/run gecikmeleri bu kabul koşusunda henüz ölçülmedi.
Mevcut kaynak Qwen3.5-9B fp8_per_tensor profilleri içerir; kullanılacak
profile/pin/backend gerçek rezervasyon öncesi yeniden doğrulanmalıdır.
Yeni model indirme veya tam eğitim yapılmaz. Lisans ve genel CI işleri
bu koordineli kabulden ayrı izlenir. M0 goal aktif, tamamlanmadı.

### Native pending expected-row CAS: R6

`af4864d` kaynaklarından ayrı test kopyası ve normal typed admission ile
`4ae4f3d7-8c86-4c0a-8865-a6746694d96b` koşusu yürütüldü. Gerçek Scorer
rolü/invocation'ı üç SAVEPOINT çağrısında yalnız beklenen attempt,
admitted_generation ve execution hash değerlerini değiştirdi. Üçü de
`P0001 / child changed after exact drain proof` ile reddedildi; job,
closure, drain, score ve outcome snapshot'ları değişmedi. Sonraki normal
RPC/kapanış **21 observer kontrolü ve exit0** ile geçti. Bağımsız READ ONLY
readback: stopped/drained/completed, generation1, sıfır aktif job,
claim/lease NULL, özgün running claim korunmuş, tek outcome ve completion.
[Native expected-row kanıtı](review-evidence/native-stop036-expected-cas.json).

Bu ayrı kopyada claim sonrasında beş saniyelik sınırlı test beklemesi ve
RPC öncesi prob vardır. Özgün SQL0036, ACL, invocation/generation guard'ları,
admission kilidi ve deadline korunur. Üretim zamanlaması veya gerçek AOS/GPU
kabulü değildir. Scorer'ın completion tablosunu okuma yetkisi yoktur;
negatif prob bu tabloyu okumaz. Final tek completion, mevcut owned migrator
rolünün salt okunur bağlantısıyla ayrıca doğrulandı. Her negatif çağrı için
completion snapshot değişmezliği ölçülmüş gibi sunulmaz.

Başarısız R1/R2 hazırlık/admission denemeleri, stop öncesinde işi tamamlanan
R3/R4 ve ACL dışı test SELECT'inde kalan R5 korunur. R5'in stop_requested
kaydı yeni deadline veya SQL müdahalesiyle kapanmış gösterilmedi. Yalnız
exact owned test API durduruldu; ana API/Director/console kimlikleri aynı.
Runner48.603s, CPU4.984s, peak354.4MiB/swap0; bu değerler tüm host/Scorer
toplamı değildir. Model/GPU çalıştırılmadı; VRAM tepe ve devir gecikmeleri
ölçülmedi.

Kalan: gerçek control-generation değişimi, doğal deadline expiry, native
RPC retry ve shared-drain successor kabulü. Expected-row generation CAS
kanıtı gerçek control-generation yarışını kapatmaz. AOS ortak runtime
capability/cancellation teyidi ve tek koordineli GPU kabulü ayrıca açık.

### Doğal closure expiry: preserved R5 fixture

`afc1004f-e2b3-406d-a6ed-c25877076c67` koşusunun özgün pending closure'ı
doğal olarak süresini doldurdu. Yeni canonical Scorer birimi gerçek
MainPID/invocation/cgroup ve least-privilege rolüyle mevcut exact job'u
RPC'ye verdi: **P0001 / child changed after exact drain proof**, actual wait
exit0. Diğer pre-expiry CAS koşulları ve generation/owner/execution bağları
geçerliydi; job/closure/drain/score/outcome snapshot'ları değişmedi.
Director'ın ayrı salt okunur gözlemi özgün first-stop+120 süresinin de
dolduğunu doğruladı; bu RPC'nin first-stop SQL guard'ını test etmiş sayılmaz.
[Expiry kanıtı](review-evidence/native-stop036-expiry-rejection.json).

Probe1.009s/145.8MiB/swap0; exact probe birimi ve cgroup kapandı. Pending
deney veya claim yeni deadline ile toparlanmadı, stopped diye etiketlenmedi.
GPU release/cleanup kanıtı yoktur. İlk helper'da olmayan lab.runs owner
kolonlarına SELECT42703 hatası korunur; yeni helper gerçek generation
tablosunu okur. ACL/schema/trigger/owner değişmedi. Completion tablosu
Scorer ACL dışında olduğu için probda yine okunmadı. Bu instrumentation
kaynaklı fixture üzerinde native closure-expiry kabulüdür; üretim crash
toparlanması, actual control-generation yarışı veya AOS/GPU kabulü değildir.

### Yeni AOS kütüphaneleri: şekil uyumlu, runtime bağlantısı açık

Scientist5da7c76 / AOSed6e857 kaynak çifti ve on bir dosya hash'i inceleme
boyunca sabitti. Yeni `scientist_lab` start/status/stop/report rotaları,
bütçeler/external ID'ler, optional purpose ve terminal report hash/status
şekilleri mevcut Scientist API ile uyumlu; yeni wire/API biçim uyuşmazlığı
bulunmadı. [Kaynak incelemesi](review-evidence/aos-new-library-source-review.json).
Bu bir deployed runtime snapshot veya admission teyidi değildir.

Gerekli bağlantılar: gerçek origin=aos API principal; güvenilir güncel
host authority/human approval; HTTP start/stop için ayrı kalıcı effect
journal ve lost-ACK reconciliation. AOS0018 yalnız inference intent
journal'dır. receipt_recorded bilinçli olarak yeni turn açmaz. Scientist'ın
mevcut principal-bound durable replay/drain/quarantine mekanizmaları korunur;
UDS hâlâ yalnız infer kabul eder, dış capability/status/reconcile/cancel
yüzeyi uygulanmadı. Socket kapanması cancellation veya GPU release değildir.

Tarihsel shared-gpu-turns/lab-external CLI/worker yolları gerçek checkout'ta
hâlâ yok. Mevcut compatibility checker o tarihsel adapter profiline bakar;
bu yollar yeni typed task adapter için evrensel zorunluluk değildir. Yeni
adapter için gerçek task/policy bağlantısı, ortak capability/version ve
cleanup kanıtı gerekir. aos-scientist-runtime.v1 / wire1 önerisi henüz
ortak runtime olarak admitted değil. AOS kodu/DB/süreçleri değiştirilmedi,
AOS testleri çalıştırılmadı; push/merge/deploy yapılmadı.

### Native terminal-row retry: R7 committed readback

Yeni `18cbe921-9ed4-4145-98a9-e308f4c109f4` CPU fixture'ında normal ilk
kapanış RPC'sinden sonra eski running expected-row çağrısı P0001 ile
reddedildi. Güncel failed expected-row ile iki tekrar failed döndürdü;
job/closure/drain/score/outcome snapshot'ları değişmedi. Probe ilk RPC'nin
transaction'ı içindeydi; ardından normal commit, otomatik recovery ve ayrı
Migrator READ ONLY readback tamamlandı: stopped/drained/completed, tek
outcome ve completion, sıfır aktif job, claim/lease NULL, özgün canlı claim
receipt'i korunmuş, API report hash'i eş.
[Retry kanıtı](review-evidence/native-stop036-retry-proof.json).

21 normal gözlem kontrolü ve native tekrar kontrolleri geçti; actual wait
exit0, 48.529s, runner peak354.3MiB/swap0. Beş saniyelik post-claim barrier
ve retry hook yalnız dondurulmuş test kopyasındadır; üretim SQL'i değişmedi.
Bu eski payload'ın körlemesine kabulü değil, eski CAS reddi ve güncel
terminal-row retry idempotence kanıtıdır. Actual G1→G2 yarışı, shared-drain
successor, gerçek model/GPU/AOS kabulü açık kalır. Ana API/drain invocation
kimlikleri aynı, test API inactive/MainPID0; GPU release ölçülmedi.

### Resumed-stop sahiplik aktarımı

Kaynak incelemesi resumed Director'ın dönüşünde `admitted_owner` olmadığını
gösterdi: normal launcher durdurma recovery'sine generation bağını veremiyordu.
Regresyon önce exit1/üç failure; iki dönüş yoluna gerçek `receipt.owner`
bağı eklenince resume-entrypoint/automatic-stop testleri 41passed/exit0.
Normal dönüş ve exception-stop aynı yakalanmış owner tuple'ını aktarır;
generation/original deadline/SQL/fencing değişmedi. Test launcher üzerinden
G2 tuple aktarımını ve özgün owner_exit_code1'in korunmasını doğrular.
[Regresyon](review-evidence/resume-owner-stop-regression.json).
Bu CPU regresyonudur; actual G1 crash→G2 resume native kabulü değildir.
Normal caught failure kapanışı resume'a uygun değildir; native G2 testi
yalnız hard crash sonrası running/active durumundan normal deadproof/drain
ile ilerlemelidir. Üretime etkinleştirilmedi.

### AOS oturumuna aktarım özeti

- Öneri `aos-scientist-runtime.v1 / wire1`; ortak admitted runtime henüz yok.
  Yeni typed adapter'ın API/wire kaynak şekli uyumlu. Tarihsel worker/CLI
  yolları gerçek checkout'ta yok; yeni adapter için evrensel şart değiller.
- Scientist principal/generation-bound replay, SQLite scheduler ve quarantine
  korunur. UDS yalnız infer; dış capability/status/reconcile/cancel bağlantısı
  ve AOS'un trusted authority/approval + durable HTTP effect journal'ı açık.
- Stop alınması, idle/quiesce, socket kapanması veya caller receipt release
  kanıtı değildir. İki taraf aynı sürüm/identity/deadline üzerinde uzlaşmalı;
  belirsiz cleanup tahsisi serbest bırakmamalı.
- Entegre GPU koşusunu yalnız Scientist yönetir. Yeni kaynaklar hazır olunca
  immutable commit/diff/source hash'leri, gerçek adapter entrypoint ve runtime
  capability/cleanup kanıtı paylaşılmalı. Bu tur AOS'ta değişiklik yapılmadı.

### Bu teslimin kalite kapısı

Actual terminal `b90b35` / exit0: yedi komut sıfır, 1319passed/7skipped/
120deselected/33warnings; wheel ve import smoke geçti. Runner78.614s,
584.1MiB/swap0. [Kaynak bağı ve önceki hatalar](review-evidence/resume-owner-quality-gate.json).
İlk deneme `/tmp` tmpfs'nin 20GiB disk rezervini karşılamamasıyla, ikinci
deneme uzun home TMPDIR'nin UDS sınırını aşmasıyla başarısızdı; ikisi
korundu. Son deneme home diskinde kısa `data/runtime/qg3` pytest basetemp
kullanır; ürün korumaları ve test beklentileri değiştirilmedi.

### Actual G1→G2 fence: dar kanıt geçti, koşu başarısız

Scientist29c0685 kaynak kopyası/owned PG55545 üzerinde typed yeni run,
canonical G1 ve sıfır fiziksel/durable child doğrulandı. Root yalnız exact
pidfd ile kendi G1'ine SIGKILL gönderdi; nonzero waiter korunur. Üretim
deadproof/drain/resume gerçek canlı canonical G2 oluşturdu; özgün execution
hash/deadline aynı kaldı. Gecikmiş G1 failure-close RPC P0001 ile reddedildi;
G2/event/report snapshot değişmedi. 20 kontrol geçti, fakat overall exit1.
[Dar nesil kanıtı ve başarısızlık](review-evidence/native-controlgen036-partial.json).

G2 sonraki calibration okumada `run has no frozen baseline calibration`
P0001/ProgrammingError ile failed oldu. CLI'nin mevcut invariant'ı resume'da
eksik calibration'ı yeniden bootstrap etmez; bu koşu o önkoşulu sağlamadı.
Tam stop/toparlanma kabulü değildir. Bir sonraki yeni fixture G1'i gerçek
frozen calibration sonrası durdurmalıdır; bu failed koşu canlandırılmaz.
İlk R2 denemesi Python pidfd fonksiyonları yokken sinyal öncesi başarısızdı;
R3 host'un mevcut libc pidfd fonksiyonlarıyla exact process fence'i korudu.

R3 runner101.929s/318.8MiB/swap0; test-only pre-baseline barrier30s ve
after-claim hook5s kaynakta (Scorer hook'a ulaşılmadı). R2 run stop_requested,
R3 failed/reportNULL; ikisinde active jobs0. Ayrı READ ONLY/physical kontrol
G1/G2 süreçlerinin öldüğünü ve cgroup'ların boş olduğunu doğruladı; bu
terminal stopped, GPU release veya başarılı araştırma kanıtı değildir.
Ana API/drain invocation aynı, own test API inactive/MainPID0; deploy yok.

Kaynakta ikinci açık kapsam: `0031_stopped_proposal` ve `stopped_proposal.py`
yalnız complete calibration + **zero candidate admission** şeklini kapatır;
proposal score-job varsa shape guard reddeder. İnflight proposal iptalini
bu dar kapanış gibi göstermemek gerekir. Bağımsız Scorer outcome/ledger,
exact process drain ve terminal finalization birlikte uygulanıp native
kanıtlanmadan geniş stop/toparlanma kabulü tamamlanmış sayılmaz. Bu tur
bu invariant gevşetilmedi; kaynak inceleme bulgusudur.

### AOS0019 HTTP journal: güncel aktarım

Kaynak çiftinin ve 19 dosyanın hash'leri incelemede sabitti. AOS artık ayrı
HTTP approval→intent commit-before-POST journal'ı ve optional console/UI
service bağlantısı içeriyor. Lost ACK intent'i korunuyor; factory injection
ve capability varsayılan ret hâlinde. Önceki in-memory-only HTTP gap kaydı
bu kaynak ilerlemesiyle güncellenir; deployed/admitted runtime değildir.
[Kaynak incelemesi](review-evidence/aos-http-journal-source-review.json).

AOS oturumuna aktarılacak somut bulgu: expired pending/approved **unsent stop**
approval unique open-action indeksini tutuyor; deadline kontrolü reject'i
de engelliyor. Yeni stop approval engellenebilir. AOS kendi CPU regresyonunu
ve yalnız gönderilmemiş audit kayıtları için atomik expiry geçişini eklemeli;
uncertain intent asla expire/reset edilmemeli. Bu kaynak çıkarımıdır, bu
oturumda AOS testi çalıştırılmadı. Trusted lost-ACK reconciliation, origin=aos
principal/registry/deployment/version teyidi ve infer cancellation/drain
kontrol yüzeyi hâlâ açık; HTTP stop ACK GPU release kanıtı değildir.
