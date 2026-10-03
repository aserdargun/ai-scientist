# AOS evidence socket entegrasyonu

2026-10-01. Scientist tabanı `15b6c8f548f324610c805c24279973d83dc58e02`;
AOS salt okunur HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Bu teslim mevcut authenticated control socket'e kanıt okuma yolunu bağlar.
Canlı servis kurulumu veya gerçek GPU kabulü değildir.

## Ortak sürüm ve gerçek endpoint

Namespace `aos-scientist-control-evidence.v2`, integer version2; yalnız
`capability` ve `reconcile`. Aynı peer/SO_PEERCRED doğrulaması, bounded frame,
call deadline ve mevcut SQLite arbiter kullanılır. Yeni socket veya GPU
tahsis otoritesi yoktur. Ana infer/control-v1 biçimi değiştirilmez.

Tam kapalı [taşıma şeması](contracts/evidence-transport-v2.schema.json)
canonical SHA:
`7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`.
Evidence container şeması SHA:
`aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`.

AOS `schemas/scientist_evidence_transport.schema.json` ile tam JSON
dokümanı aynı çıktı; bağımsız canonical hash yukarıdaki değerdir. AOS dosya
hash'i `0a18309a4c19824b7753179220eda840e7a8a545aea09b54023cfcce15737f99`.
Bu şema eşleşmesi source/config/authority veya runtime kabulü sağlamaz.

İstek sekiz v1 alanının yanında zorunlu `evidence_schema_sha256` ve
`transport_schema_sha256` içerir. Target üç alanıyla her iki işlemde zorunludur.
Capability yanıtı özgün **persist edilmiş** v1 target capability/hash'ini
korur; iki pin yalnız yanıt data'sına eklenir. Infer bootstrap capability,
bilinen bir hedef için bile kanıt okuma yetkisi değildir.

Private policy'de iki alan birlikte açıkça pinlenmelidir:
`evidence_schema_sha256` ve `evidence_transport_schema_sha256`. Eksik çift,
yanlış pin veya eksik codec/contract kaynak pinleri reddedilir. Eski config
bu endpoint'i kapalı tutar. Policy değişikliği eski admission hash'ini
yenilemez; mevcut hedef farklı policy'ye aitse açık target cleanup ACL gerekir.

Reconcile aynı reserved kontrol kotasını kullanır; current generation,
retained target authority, expiry ve release/no-admission zinciri aynı
transaction içinde doğrulanır. Çıktı framing/shape doğrulaması quota commit'inden
öncedir. Capability yanıtı da transaction içinde doğrulanır; yalnız status
ACL'inden evidence capability oluşturulamaz. Son yavaş policy okumasından
sonra expiry denetlenir. Socket publication öncesinde current policy ve
reconcile authority tekrar doğrulanır.

## İcra kanıtı

Gerçek socketpair + SQLite testinde sentetik authenticator kullanılarak
register → v1 cancel → evidence capability → reconcile → exact retry geçti.
Özgün canonical terminal bytes ve hashes korundu; iptal sonucu null kaldı.
Normal current ve açık reconcile ACL'li retired policy yolları çalıştı.
Aktif GPU tahsisi oluşturulmadı. Bu CPU akışı native AOS/GPU kabulü değildir.

Üç inceleme bulgusu önce **3 failed / parent exit1 / source unchanged**
ile üretildi: son policy okumasında expiry; reconcile'sız retired ACL'nin
commit sonrası reddi; son socket peer gözleminde policy revoke. Düzeltmeler
sonrasında **110 birleşik CPU kontrolü geçti / parent exit0 / source unchanged**.
Parent süre 18.555800035 saniye. Zorunlu kalite kapısı **1717 passed /
7 opt-in skipped / 120 GPU-live deselected**; yedi komut exit0. Strict mypy
151 dosyada geçti; wheel codec ve önceki contract artefaktlarını içeriyor.
Parent 109.270387476 saniye; tüm Python kaynakları koşu boyunca ve teslimde
aynı hash'te. Kaynak/diff/komut hash'leri ve sınırlar
[makine kanıtında](review-evidence/aos-evidence-socket-source-delivery.json).

## Güncel AOS kaynağı ve kalan işler

`evidence_transport_candidate_v2` gerçek observer'ın 38 dosyalık seçimidir:
admission33 + terminal2 + evidence3. Ön kontrol source profillerini birbirine
karıştırmaz. Salt okunur koşu **exit2 / unsupported / admission_allowed=false**:
tracked checkout dirty ve gerekli kaynaklar untracked. Seçili SHA gözlemi:
`f1d3d2c490da50cbf256b5c6d237cdd8aa9918761a155bc1ef785a343ce5b359`.
Bu pin otomatik güncellenmez; kaynak değişirse yeniden review gerekir.

AOS oturumuna beklenti: bu exact transport/evidence pinleriyle mevcut
endpoint'e bağlan; original history/budget ve request/output bundle'ı bağımsız
tut. Decoder sonrası current resolver/retained-target authority ve fiziksel
allocation/drain preimage'larını doğrula; ancak sonra journal resolution yap.
Idle/quiesce veya stop ACK release değildir. Yeni kontrollü GPU denemesi
Scientist tarafından tek yürütücü olarak, ortak committed kaynak/config ve
mevcut scheduler rezervasyonu doğrulandıktan sonra başlatılacak.

Model/backend/quantization/context çağrısı, VRAM tepe, gerçek GPU bekleme/devir
ve çalışma gecikmesi bu teslimde ölçülmedi. AOS dosyası/süreci, eski quarantine,
canlı servis ve kullanıcı işi değiştirilmedi; push/merge/deploy yapılmadı.
Native journal resolution ve koordineli GPU kabulü açık. Kabul sayıları
11 passed / 7 partial / 4 open olarak kaldı; lisans/genel CI ayrı işlerdir.
