# Aynı authenticated socket üzerinde özgün bütçe aktarımı

2026-10-01. Scientist tabanı `dcaedf7ef955b3753e09fdba86ec0dbe850d50a7`.
AOS kaynak gözlemi HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`;
AOS dosyası/süreci değiştirilmedi.

## Somut bağlantı

AOS handoff'undaki "özel Scientist DB'sini okumadan authenticated budget
transport" eksikliği için mevcut kontrol socket'ine açık opt-in
`aos-scientist-control-evidence.v3` / integer3 eklendi. İki operasyon aynı:
`capability` ve `reconcile`. Yeni listener, allocator, target authority veya
scheduler tablosu yok. Infer/control-v1 ve evidence-v2 değiştirilmez.

V3 reconcile data tam olarak `{evidence, original_budget_witness}` olur.
Evidence mevcut terminal-evidence.v2 container'ıdır; witness mevcut
original-budget-witness.v1 şeklidir. Terminal-v1 canonical bytes, float
CLOCK_BOOTTIME saatleri ve original admission pinleri korunur. Witness,
terminal içinden türetilmez: ilk intent/allocation/readiness sütunlarından
bağımsız üretilir ve aynı transaction içindeki terminal bütçesiyle karşılaştırılır.

`read_retained_terminal_evidence` tek `BEGIN IMMEDIATE` içinde original owner,
target, reserved original budget ve retained reconcile authority doğrular.
Aynı row'dan iki private builder ile snapshot oluşturulur. Full byte bound ve
socket response validator ID/quota yazımından önce, son authority doğrulaması
commit öncesindedir. Callback, authority, byte kapasitesi veya proof hatası
transaction'ı geri alır. Exact retry yeni budget/deadline üretmez.

Codec ayrıca request/terminal/witness/retained capability target, original
admission/cleanup, profile/deployment/config/content ve allocation bağlarını
karşılaştırır. Capability caller'dan uydurulmaz; producer existing persisted
target-capability table'dan okur. Orijinal infer bootstrap capability proof-export
yetkisi değildir. Final socket publication mevcut current-peer/policy/authority
kapılarını tekrar kullanır. Cleanup proof üretmez veya tahsisi release etmez.
Null-budget tombstone ve allocated/no-child release hâlâ desteklenmez.

## CPU icra kanıtı

75 odaklı kontrol / parent exit0 / 6.360591402 saniye / kaynaklar değişmeden:
aynı authenticated socket/SQLite üzerinde iptal edilmiş original intent için
capability → combined reconcile → exact retry çalıştı; aynı-key tekrar kota
artırmadı. Aynı original capability ile v2 export aynı inner evidence'ı verdi.
Authentication ve physical drain gözlemleri sentetik fixture'dır; bu gerçek
AOS/model/GPU acceptance değildir. Completed/released ve canceled store
snapshot'ları, concurrent writer lock, validator/final revoke rollback,
yanlış owner/target, inflight/quarantine, rehashed receipt budget tamper,
null budget/nochild ve full combined byte bound denetlendi.

Yeni final-encoding deadline regresyonu önce gerçekten 1failed verdi. Düzeltme
sonrasında encoding/policy/generation gözlemlerinden **sonra** original call
deadline yeniden ölçülür; bütçe dolmuşsa yanıt gönderilmez. Bu kontrol yeni
socket deadline üretmez. Publication hatası daha önce commit edilmiş control
ledger'ını temizlemez; caller belirsizliği durable reconcile ile ele almalıdır.

İlk zorunlu kalite kapısında mevcut iki-process fairness testinin cleanup'ı,
ilk üç sonucu okuduktan sonra kalan AOS backlog bitmeden worker'ı SIGTERM ile
sonlandırdı (parent exit1 / 1779 passed, 1 failed). İlk adil sıra kontrolleri
geçti; scheduler kaynakları değiştirilmedi. Test şimdi aynı sekiz saniyelik her
progress beklemesiyle 31 AOS + 1 Lab turn'ün tamamını alır ve tam kimlik kümesini
doğrular; sonra join yapar. Odaklı süreç kontrolü exit0 / 3.095982796 saniye;
fairness sırası ve starvation şartı gevşetilmedi. Başarısız kapı kanıtı korunur.

Zorunlu kalite kapısının son koşusu **1780 passed / 7 opt-in skipped / 120
GPU-live deselected**; yedi komut exit0, parent149.833253270 saniye. Tüm Python
kaynakları koşu boyunca ve teslim kontrolünde aynı hash'te. Başarısız önceki
kapı, deadline RED ve final odaklı/full kanıtlar
[makine kaydında](review-evidence/aos-retained-evidence-source-delivery.json).

## Açık opt-in sözleşme

Tam self-contained [public v3 şema](contracts/retained-evidence-transport-v3.schema.json)
canonical SHA:
`cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070`

Inner evidence schema SHA değişmedi:
`aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`

Mevcut v2 full transport SHA değişmedi:
`7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`

Private policy'de mevcut evidence schema/v2 transport pinleri yanında yeni
`retained_evidence_transport_schema_sha256` exact v3 pinini gerektirir.
Scientist source closure yeni codec dosyasını da kapsar. Pin yoksa v3 reddedilir;
varsayılan v2 davranışı yükseltilmez. Request'teki `transport_schema_sha256` v3
hash'idir; `evidence_schema_sha256` aynı inner hash'tir. V3 successful reconcile
validation, ayrıca exact `expected_capability` retained preimage ister.

## AOS oturumuna aktarım ve kalan iş

Scientist mevcut socket üzerinde v3 candidate'ı sağlayacak; AOS eski v2 client'ı
bu sürümle otomatik uyumlu değildir. Tam public schema ve yukarıdaki pin karşılıklı
review edilip explicit namespace seçilmelidir. AOS authenticated client/journal
wire response ACK'sini original admission/target'a bağlı immutable saklayabilir;
provider `json.loads(original_budget_witness.budget_canonical)` değerini ayrı
`expected_budget` olarak kullanır. Terminal budget'ını bu alana kopyalamaz.

Current target-rights/resolver, bağımsız physical-proof validator, pinned result
validator ve durable inference resolution yine zorunludur. ACK veya journal
pending-count=0 GPU sahipliğini çözmez. Caller restart/transfer kabul edilmez.
Koordineli GPU koşusunu yalnız Scientist oturumu yürütecek; broker config,
source/config review, mevcut scheduler rezervasyonu ve kullanıcı işiyle
çakışmama ayrıca doğrulanmalıdır. Bu teslim deploy, gerçek AOS journal uyumu,
araştırma başarısı, VRAM kapasitesi veya eğitim sonucu değildir.

## Son paralel AOS değişikliği

Teslim sırasında AOS retained terminal/release-proof adaptörünü ekledi ve ayrı
`release_proof_candidate_v1` 48 kaynak profili istedi. Önceki journal44 exact
pinlerinin yeniden kontrolü selected source/tracked diff/selected untracked
uyuşmazlığıyla **unsupported** verdi; pinler otomatik güncellenmedi. Önceki44
başarısı tarihli kanıttır. Yeni48 üyelik ve actual source review sonraki ortak
uyumluluk adımıdır; bu v3 teslimi o yeni AOS kaynağını kabul etmiş sayılmaz.

Sonraki salt okunur gözlemde AOS v3 codec/journal migration0025 ve
`retained_evidence_candidate_v3` 52 kaynak profilini de ekledi. Tam v3 public
schema JSON ve canonical SHA Scientist ile aynı; bu structural agreement
gözlemidir, source52/config/provider/runtime kabulü değildir. Yeni52 profil
incelemesi önceki48 isteğinin sonraki sürümüdür.
