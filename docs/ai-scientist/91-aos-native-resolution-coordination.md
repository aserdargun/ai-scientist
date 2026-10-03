# Güncel AOS native kabul bağlantısı ve durable resolution eksikliği

2026-10-01. Scientist HEAD `fa05289e07c7e3a6d9a859dfde9f60d8d021422f`.
Salt okunur AOS gözleminde v3 codec + migration0025 + release-proof/budget
adaptörleri var; kaynak profili `retained_evidence_candidate_v3` (52 dosya).
Tam ortak schema pin `cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070`.

## Gerçek native akıştaki eksik

AOS `ScientistIntentJournal.record_receipt()` yalnız `receipt_recorded` yazar.
`ScientistEvidenceJournal.record_response()` immutable control ACK saklar;
`inspect()` pending/ACK görünümüdür. İncelenen kaynakta inference resolution
API'si yok. `ScientistDesktopScheduler._scientist_admission()` herhangi bir
original `scientist_turn_intents` kaydı bulunduğunda yeni admission'ı reddeder;
`verify_admission()` aynı session'da farklı request'i de reddeder. Dolayısıyla
başarılı reconcile ACK veya verified terminal tek başına sonraki native turu
açmaz. Bu gate'i bypass etmek, intent silmek veya DB/session değiştirmek gerçek
birlikte ilerleme kabulünü sağlamaz.

## AOS oturumuna somut beklenti

Original infer journal için ayrı append-only **resolution API** ve bu sonucu
okuyan explicit admission gate gerekiyor. API verified terminal evidence +
independently retained budget + result validator + bağımsız physical cleanup
sonucunu, güncel retained-target/resolver hakkını doğrulayan trusted composition
üzerinden almalı. Özgün kayıtlar veya sayaç/bütçeler değişmemeli.

Bağlar: exact original infer bytes/hash, request/profile/deployment, original
admission binding/hash, caller/server generations, session/runtime/owner/lease/
generation/context hash, retained capability preimage/hash, control ID/bytes/hash,
authenticated broker peer, original budget/allocation/child/terminal/result hashleri.
Current authority ve original rows aynı tutarlı transaction'da son kez doğrulanmalı.
Wrong owner/stale generation/revoked grant veya mismatched ACK reddedilmeli.
Exact repeated resolution idempotent olmalı; farklı receipt aynı original request'i
çözmemeli. Başka pending control/inference veya quarantine varsa admission kapalı
kalmalı. Kaynak allocation release authority'si Scientist'in mevcut scheduler'ıdır;
AOS resolution yeni GPU otoritesi değildir. Release kanıtlanmamışsa çözüm yazılmaz.

Bu, karşı oturuma uygulanacak öneridir; AOS'a dosya/DB yazılmadı. Üretim API'si
varmış gibi Scientist wrapper yazılmayacak. Karşı tarafın yeni kaynakları ayrıca
review edilmiş profile/config closure içinde değerlendirilecek.

## Scientist-owned kabul composition sırası

1. Aynı izole AOS `TrajectoryStore` üzerinde immutable admission history2.0.
2. `ScientistRetainedEvidenceCodec` → existing authenticated evidence client →
   aynı store'un evidence journal `authorize/persist_intent/record_response` callback'leri.
3. Retained capability keşif ACK'si saklandıktan sonra reconcile için exact
   capability preimage; terminalden kopyalanmamış authenticated budget witness.
4. Budget witness verifier + retained terminal verifier; current source/resolver,
   original-target rights, pinned profile result validator ve bağımsız owner/fence/
   nonce/child/cgroup/GPU fiziksel readback provider'ları zorunlu/default deny.
5. AOS yeni durable resolution API'si varsa verified result ile transaction;
   ACK persistence veya pending-count=0 bu işlemin yerine geçmez.
6. Sonraki AOS/Lab turunun original admission gate üzerinden ilerlediğini doğrula.

Typed Lab yolu ayrı: `prepare_scientist_lab_startup` → `ScientistLabService.propose`
→ exact envelope hash ile `respond` → `execute_async` → `read_async(...,'lab.report')`.
Scientist dış Scorer/rapor hash doğrulaması ve uzun işin foreground scheduler'ı
bloklamaması korunur. Bir model yanıtı araştırma/proposal/scoring kabulü değildir.

Kabul koşusu ancak actual source/config/principal, tek canonical scheduler
rezervasyonu ve aktif kullanıcı işiyle çakışmama doğrulandıktan sonra başlar.
Model/backend yeni indirmeden mevcut ölçülmüş artefaktlardan seçilir. Bu not
runtime/deploy/model/GPU icrası veya M0 tamamlandı iddiası değildir.

## Actual source52 gözlemi ve icra

AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Selected52 canonical manifest SHA
`8100b7763c20b940f464e3a2d95bd42d00bda4e6be114ca2ebc6a71cc149cb02`;
tracked binary diff SHA
`9cc14726ea5a0612b3e7ee2f18ea007cadcc48e7aa55ba11af55f63a70bb6167`;
selected untracked manifest SHA
`3c9cb4191a5d023837013c103e026e64e7be8761c53aa63898f99425bc7542c4`.
Bu beklenen pinlerle CLI exit3/pending/source_ready=true; admission_allowed=false.
Eski current_checkout default'u dirty/untracked retlerini korudu.

48 ve52 üyelikleri ayrı literal AST zincirlerinden okunur; eski38/39/44
üyelikleri değişmez. Yeni dört verifier/codec sınıfı, schema dosyaları ve SQL0025
manifestte zorunludur. V3 tam schema hem explicit ortak hash'e hem yerel
Scientist public artifact'ına eşit olmalıdır. AOS'un ayrıca kullandığı daha sıkı
request schema'nın independent canonical SHA'sı
`141896bab866fbc005c7b638be265e9496ed47b0895e8dbaae91b3952e08d802`.
Bu yerel request schema tam transport schema gibi sunulmaz.

73 portable CPU kontrolü geçti (parent exit0 / 4.671197488 saniye / kaynaklar
sabit). Required file/member/marker drift, reviewed bytes olsa dahi yanlış full
schema/request version, duplicate JSON ve observer tuple retleri denetlendi.
Testler AOS checkout'u olmadan çalışır. Bunlar source compatibility kanıtıdır;
AOS modeli, API, DB veya GPU çalıştırılmadı. Kabul toplamı11 passed/7 partial/4
open değişmedi. Lisans/genel CI bu native integration gate'in dışında tutulur.

Zorunlu kalite kapısı:1795 passed / 7 opt-in skipped / 120 GPU-live deselected;
yedi komut exit0, parent117.415506285 saniye, Python kaynakları sabit.
[Commit tabanı, local diff hashleri ve CPU kanıtları](review-evidence/aos-retained52-source-delivery.json).
