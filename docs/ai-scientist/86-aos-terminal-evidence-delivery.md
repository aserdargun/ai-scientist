# AOS kapanış kanıtı: kaynak teslimi

2026-10-01. Bu teslim CPU/kaynak teslimidir; gerçek GPU kabulü değildir.
Sonraki [authenticated socket teslimi](87-aos-evidence-socket-integration.md)
kanıt okuma yolunu ayrı namespace altında bağlar; aşağıdaki socket eksikliği
bu önceki teslimin tarihsel sınırıdır.
Scientist tabanı `ebb8f5dfe1b1dc48e45fbb18cc7f42cf20bf0217`;
salt okunur AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Teslim commit'i Git geçmişinden ve aşağıdaki kaynak hash'lerinden belirlenir.

## Çalışan parça

`ControlStore.read_terminal_evidence` aynı SQLite transaction içinde mevcut
retained-target authority'yi ve özgün owner/request generation'ı doğrular.
Terminal durum olmadan kanıt dönmez. Release kanıtında özgün allocation fence,
bağımsız kayıtlı child generation, drain ve aktif tahsisin yokluğu denetlenir.
Kanıt eksikse yeni bir release veya recovery işlemi yapmaz.

Çıktı `aos-scientist-terminal-evidence.v2`, integer `version: 2` içerir.
Kapalı alanları `target`, `terminal_canonical`, `allocation_canonical`,
`drain_canonical`, `no_admission_canonical`, `result_canonical`'dır.
Preimage'lar özgün canonical JSON **string** olarak korunur. Eski v1 float
saatleri ve hash'leri yeniden yazılmaz. Bu kapsayıcı eski receipt'i yeni v2
admission veya release yetkisine dönüştürmez.

Completed sonucu yalnız özgün receipt/result zinciri doğrulanırsa döner.
Canceled/expired/failed kayıtları bekleyen eski sonuç bulunsa bile sonuç
yayımlamaz. Never-admitted kanıtı allocation/child/drain veya aktif hedefle
birlikte kabul edilmez. Özgün budget, assigned deadlines ve child generation
receipt ile bağımsız retained row arasında eşleşir. Allocation preimage'ı da
özgün request hash/budget/envelope deadline ile eşleşir.

Reconcile mevcut ayrılmış kontrol kotasını kullanır. Aynı ID tekrarında
ek kota harcanmaz. Son authority doğrulaması commit öncesindedir; geç revoke
ID/kota değişikliklerini rollback eder. Yeni GPU tahsis otoritesi yoktur.

## Tam şema adayı ve sınır

`lab/llm/contracts/control_v2/` sekiz kapalı şema ve bundle içerir. Saf
`aos_control_contract_v2` doğrulayıcısı request, capability, binding, cleanup
grant, terminal, response, legacy terminal ve evidence ilişkilerini denetler.
Yeni metadata için integer CLOCK_BOOTTIME mikrosaniyesi/boot kimliği açıkça
tanımlanmıştır; eski kayıtların saatleri yenilenmez. Response şeması özgün
infer bytes ve output bundle'a göre özelleştirilir.

Canonical bundle SHA:
`8dd9dbfccd2c0cc93d4ab556b06ee4b1fe21ebe85e11616dd046cae1926a65f2`.
Evidence şeması canonical SHA:
`aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`.

**Socket v1 değiştirilmedi.** Store API'si trusted host composition içindir;
unauthenticated export endpoint değildir. Yeni evidence transport, current
resolver/target-bound capability ve AOS journal resolution henüz bağlı değil.
Saf şema doğrulaması fiziksel GPU cleanup veya ACL doğrulaması sayılmaz.
Allocated fakat hiç child başlamamış kayıt bu API'de hâlâ konservatif ret alır;
bu varyant için kabul iddiası yoktur.

## İcra ve güncel AOS engeli

Birleşik bounded CPU koşusu **78 passed / exit0**, kaynaklar değişmeden
5.920302212 saniye parent süresiyle tamamlandı. Store exporter fixture'ları,
eski terminal publication regresyonları, yeni şema ve admission_v2 kaynak
kontrolleri birlikte çalıştırıldı. Never-admitted store çıktısı yeni evidence
doğrulayıcısından doğrudan geçirildi. Fixture GPU/drain gözlemleri fiziksel
GPU kanıtı değildir. İlk koşuda yeni testin yanlış tablo adı nedeniyle 9 hata
alındı; düzeltilen koşuların gerçek exit code'ları ayrıca saklandı.

Private receipt:
`data/runtime/parallel-m0/root-attempted037-checks/terminal-evidence-interop-204a363a09a6455f8c9c683f3057c162.private.json`.
Zorunlu kalite kapısı **1686 passed / 7 opt-in skipped / 120 GPU-live
deselected**; yedi komut gerçek exit0. Strict mypy 150 kaynak dosyada geçti.
Wheel sekiz yeni şema/bundle/modülü ve önceki output-v2 dosyalarını içerir.
Parent 118.043622680 saniye / exit0; tüm Python kaynakları koşu boyunca ve
teslim kontrolünde aynı hash'te kaldı. Ayrıntılar
[makine kanıtında](review-evidence/aos-terminal-evidence-source-delivery.json).

Gerçek AOS `admission_v2` kaynak seçimi 33 dosyadır. Read-only ön kontrol
**exit2 / unsupported / admission_allowed=false** verdi:
`tracked_checkout_dirty` ve `required_source_untracked`.
Decider/Bonsai worker dosyaları mevcuttur fakat untracked'tir; public HEAD'in
parçası gibi gösterilmez. Seçili kaynak aggregate SHA:
`e8cfb75f6cd4f1b986a9f3983bd215a655753540e9fc07fab8686201a698a2e1`.
Bu gözlem temiz/atomik AOS sürüm onayı değildir.

GPU koşusu, model çağrısı veya eğitim başlatılmadı. Model/quantization/context,
VRAM tepe ve GPU bekleme/devir/çalışma gecikmeleri bu teslimde **ölçülmedi**.
Canlı servis, AOS dosyası, eski quarantine veya kullanıcı işi değiştirilmedi.

## AOS oturumuna aktarım

1. Scientist beş pinli output-v2 binding'i ve `admission_v2` seçili kaynak
   algoritmasını tanıyor; kaynakları commit/pin ettikten sonra tekrar ön kontrol
   yapılmalı. Mevcut kontrol descriptor'ı tam JSON şeması değildir.
2. Evidence bundle ve yukarıdaki kapalı container şekli öneridir. Eski terminal
   canonical bytes/hash'leri aynen tutulacak. Ana control.v2 üreticisi henüz
   devrede olmadığından v1'e sessiz alan ekleme veya sürüm yükseltme yapılmamalı.
3. Ortak evidence taşıması current resolver kimliği ve original target authority
   ile açıkça pinlenmeli. AOS verifier fiziksel preimage doğrulaması ve özgün
   budget/output kontrolünden sonra journal resolution yapmalı; idle/quiesce
   veya stop ACK release kanıtı kabul edilmemeli.
4. Tek gerçek GPU kabul koşusunun yürütücüsü Scientist oturumu. Önce bu taşıma,
   resolution ve temiz kaynak çiftini tamamlayalım; sonra mevcut sınırlı model
   yapılandırması ve scheduler rezervasyonuyla koşalım.

Genel lisans/CI işleri bu AOS kabulünden ayrıdır. M0 kabul sayıları değişmedi.
