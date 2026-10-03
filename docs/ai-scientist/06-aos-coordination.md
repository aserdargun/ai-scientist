# Paralel AOS geliştirmesi için entegrasyon notu

Tarih: 2026-09-24. Kullanıcı AOS üzerinde başka geliştirme oturumunun paralel çalıştığını bildirdi ve AOS değişikliği gereksiniminin paylaşılmasını istedi. Bu not o oturuma iletilebilir. AI Scientist uygulama ajanı mevcut AOS kaynaklarını şimdilik değiştirmiyor; değişiklikler ayrı yama/opt-in test alanında hazırlanacak.

Amaç: mevcut CachyOS, RTX 4070 Ti SUPER 16 GB VRAM ve 32 GB RAM host üzerinde AOS kullanılmaya devam ederken AI Scientist uzun araştırma koşularını ilerletebilmeli. Aynı anda bütün modellerin VRAM'de tutulacağı varsayılmıyor. Bağımlılık, port, süreç, RAM ve GPU çatışması engellenmeli.

## Güncel doğrudan koordinasyon — 2026-10-02

### Gerçek paylaşım için süre düzeltmesi

Önceki yaklaşık 300 saniyelik CPU baseline ölçümleri, 300 saniyelik native
belgeyle çekişmeli GPU testinin başlayamadığını gösteriyor. Yeni launch
girdisinde açık 1–900 saniye seçimi, varsayılan 300 ve mevcut pinli özgün
yetkiye kırpma uygulanıyor. İkinci ayrı Lab deneyi kontrollü iptal içindir;
tamamlanmış ilk deney yeniden açılmaz. [Süreler ve kabul planı](116-coordinated-acceptance-window.md).
Bu değişiklik nedeniyle aşağıdaki v1 hazırlık paketi tarihsel kalır;
yeni kaynak/config hash'leriyle v2 aktarımı hazırlanır. AOS'tan eski
paketi onaylaması veya runtime başlatması istenmez.

### Yeni v8 alanı ve kalan yaşam döngüsü düzeltmesi

AOS'un `native-coordinated-review-v8/scope-preparation.private.json` kaydı
SHA-256 `e99a9fa9c805171aabcc28adb42222a529c0346a41bb94efe8237faca68e5f0f`
ile doğrulandı: 11 boş özel dizin, iki henüz oluşturulmamış DB yolu ve 74
güncel AOS kaynak dosyası eşleşiyor. Scientist bu alanı yazmadı. APIv6'nın
tek önerili süiti hazır; eski v5/v7 saatleri ve hakları kullanılmaz.

İlk control düzeltmesinden sonra provider hazırlığı ve bütün resolution
zincirinin de süreyi aştığı CPU bileşiminde görüldü. Düzeltme tamamlandı:
özgün 3/3/30 saniye sınırları korunarak 106 doğrulama / 636 taze snapshot /
16 native Codec, gerçek AOS interpreteri üzerinde 23,916 saniyede geçti.
88 odaklı test ve tam kapıda 3002 test/yedi komut exit0 geçti;
canlı SQLite/IPC/GPU kabulü henüz çalıştırılmadı.
[Dondurulmuş kaynak ve tam kapı kaydı](review-evidence/native-retained-lifecycle-scope-20261002.json).

Güncel gerçek checkout'ta `services/decider/broker_worker.py` ve
`services/bonsai/broker_worker.py` vardır. Tarihsel `--shared-gpu-turns` ve
`--lab-external-*` bayrakları runtime kaynaklarında yoktur; mevcut native
`--engine scientist`, configured factory/retained hook ve canonical broker
yolu kullanılır. İzole eski yamalı kopya uyumluluk kanıtı sayılmaz.

Doğrudan Codex MCP mesaj kanalı `127.0.0.1:37321/mcp` adresinde erişilemiyor.
AOS'un v8 hazırlığı yeni Scientist kaynaklarına son onay vermemiştir;
dosya hazırlığı runtime/config anlaşması veya yürütme yetkisi olarak sunulmaz.
Yeni kaynak/config paketi bu proje içindeki dosya aktarımıyla paylaşılır.
GPU yürütücüsü yalnız Scientist'tir; AOS kaynak yazımı ve karşı oturumun
süreçlerine müdahale yoktur.

**Önceki v1 paket:** Scientist kaynak commit'i `4a71c6c`; APIv6 hazırlığı ve
native-v8 şablonları gerçek exit0 ile üretildi. 136 API / 141 native kaynak
pini ve bağımlı dosya hash'leri
[hazırlık kaydındadır](review-evidence/api-v6-native-v8-inert-preparation-20261002.json).
Kısa karşı inceleme notu yerelde
`data/runtime/aos-native-coordinated-20261002-v1/AOS_V8_HANDOFF.private.md`
içindedir. Doğrudan gönderim yine bağlantı hatası verdi; kullanıcıdan bu notun
AOS oturumuna aktarılması istendi. Yeni native/API servisi veya GPU işi
başlatılmadı; karşı kaynak/config incelemesi henüz alınmadı.

### Son v7 sonucu ve devam eden düzeltme

Gerçek fence25 AOS çıkarımı geçti; retained sonuç kabulü başarısız olduğu
için ilk dosya görevi tamamlanmadı ve Scientist v5 deneyi başlamadı.
[Sonuç, süreler ve fiziksel kapanış](115-native-retained-resolution-wiring.md)
kayıtlıdır. Scientist'in model/native/API/clone kapsamı temiz kapandı;
arayüz ve tünel açıktır. Eski intent veya özgün süre yeniden kullanılmaz.

AOS'un `native-diagnostic-handoff.private.json` teslimi salt okunur alındı
(06:04:19 UTC; SHA-256
`4435dfd2c95f15d928d32f22ee60a78d6c1eb05ce6386aa5f10053165fa9e989`).
Gerçek checkout'taki tek runtime değişikliği `scientist_evidence_client.py`
hata konumlarını dıştan dört/içten dört frame olarak kaydeder; kaynak hash'i
`8b0d974c8b97b31cce5d7ea923c21b8f9c3e7df97a48377c4238842cfa218e01`
eşleşti. AOS bağımsız startup/cleanup dosyalarının hash'leri de doğrulandı;
AOS bu teslimde bağımsız GPU sorgusu yaptığını iddia etmiyor. 34 odaklı ve
5304 paket testi AOS'un kendi raporudur, Scientist test sayısına eklenmez.

Scientist yalnız kendi native artifact/retained factory kodunu düzeltti.
Tek üç saniyelik control içinde tekrar edilen doğrulayıcı açılışları ve
Codec derlemesi azaltıldı; güncel yetki/iptal/fencing kontrolleri korunur.
Tam CPU fixture zinciri 2,729 saniye/exit0; 83 odaklı test ve son tam kapı
2997 test/yedi komut exit0 geçti.
[Dört dosyanın hash'i ve CPU kanıtı](review-evidence/native-retained-control-scope-20261002.json)
yeni kaynak eşleştirmesinin Scientist teslimidir; runtime izni veya ortak
GPU kabulü değildir. Model/imaj/harness değişmedi. Son mesaj
girişiminde Codex oturumlar arası MCP bağlantısı erişilemedi; cleanup
bildiriminin mesaj teslimi teyitli değildir. Bu dosya ve 115'teki kısa aktarım
güncel ortak devir kaydıdır. Yeni runtime için düzeltilen kaynak pinlerinin
aynı paket üzerinde eşleşmesi gerekir; GPU yürütücüsü yalnız Scientist'tir.

### Tekrarlı çağrı bağlantısının görev paylaşımı

İki oturum doğrudan mesajlaşıyor; AOS, mevcut control socket üzerinden
sürümlü FD aktarımını koşullu uyumlu buldu. Scientist kendi broker/client ve
configured factory bağlantısını, AOS kendi typed owner-loop hook'unu geliştirir.
Bir tarafın CPU geliştirmesi diğerinin runtime teyidini beklemez. Kaynak
sahipliği korunur; Scientist AOS dosyalarını değiştirmez.

Yeni `aos-scientist-retained-channel.v1` yalnız mevcut `read_budget` ve
`verify_physical` işlemlerine kanal taşır. Descriptor SHA-256:
`cd61387214a62ddf8fbac9f063ee2154035d9848d2c15412531f850f2ddf2600`.
Yeni listener veya GPU tahsis otoritesi yoktur. Tam bir CLOEXEC FD,
her mesajda SCM_CREDENTIALS, özgün PID/start/invocation/cgroup,
sınırlı tek kanal ve özgün üç saniyelik çağrı süresi doğrulanır.
Belirsiz aktarımda FD'ler kapatılır; kanal kendi başına control,
resolution veya release yetkisi vermez.

AOS bağlantısı: trusted `resolver_factory(binding, current_binding)` ile
oluşturulan resolver'ın `resolve_successful(request_id)` işlemi, durable
receipt sonrası ve sonraki admission öncesinde özgün owner thread'de çalışır.
Successful receipt için client rearm yapılmaz. Bağımsız budget ve fiziksel
kanıt, exact durable capability/reconcile ACK'lerinden sonra trusted
`prepare_resolution` ile bağlanır; ACK, idle veya canonical done tek başına
release kanıtı değildir.

Scientist factory ve AOS hook kaynak teslimi tamamlandı;
[güncel teslim](115-native-retained-resolution-wiring.md) exact aktif request,
özgün deadline ve bağımsız proof bağlantısını kaydeder. AOS kaynak incelemesi
uyumlu; final imajla yedi kalite komutu exit0/2959 test geçti. Bu sonuç gerçek
native tekrar kabulü değildir. APIv4 özgün süre sonunda MainPID0; APIv5
ve önce disabled, sonra karşı incelemeyle enabled yapılan v7 paketinin
denemesi yukarıdaki sonuçla kapandı. AOS kendi izole v7 alanını sağlamıştı.
Entegre GPU yürütücüsü yalnız Scientist'tir. Tekrarlı adil ilerleme ve
ayrı kontrollü iptal/toparlanma henüz kabul edilmiş değildir.


### İlk terminal araştırma ve sonraki ortak iş

[2026-10-02 gerçek koşu raporu](114-first-native-research-report.md): AOS
hello, Scientist tek sentetik snapshot araştırması, bağımsız puanlama,
AOS rapor kaydı/readback ve model/işçi fiziksel kapanışı geçti. AOS karşı
readback ayrıca doğruladı. İkinci çağrı, ilk intent için bağımsız resolution
bağlanmadığı için admission kapısında engellendi; tekrar POST yapılmadı.

Sahip olunan native servisler/broker/desktop kapatıldıktan sonra AOS kaynak
freeze'i kendi CPU geliştirmesi için kaldırıldı; eski kanıt dosyaları immutable
korunur. AOS kendi HTTP denial handling ve owner-loop retained-host arayüzünü;
Scientist kendi native factory/channel ve bağımsız fiziksel verifier bağlantısını
ilerletir. Yeni pin anlaşmasından önce GPU koşusu yapılmaz. Kuyrukta fairness,
tekrarlı iki taraflı ilerleme ve kontrollü iptal/toparlanma açık kalır.

### Son pin eşleştirmesi — APIv4 hazırlığı

APIv4 hazırlığı gerçek exit0: 0042 migration uygulandı; beş eski run ve bağlı
satırlar birebir değişmeden kaldı. Kaynak039 kapalı ve değişmeden korundu,
geçici migrator kimliği kaldırıldı. AOS canlı PID/start/invocation/cgroup ve
GET capability eşliğini bağımsız doğruladı; özgün 3600s süre değişmedi.
Karşı readback SHA: `c9b517b73e8003c30ab1777e81378390d44d4c5600ee2038e93d027862331eeb`.
Bu adım broker, model, GPU veya deney başlatmadı. Tam disabled native
paket üretildi ve karşı incelemeye gönderildi; ortak sözleşme sürümleri
korundu, yeni GPU tahsis otoritesi oluşturulmadı.


AOS oturumuna final imaj, harness ve yeni immutable suite pinleri doğrudan
iletildi. AOS `native-coordinated-review-v6` alanını CPU hazırlığı olarak
korur; bağımsız CPU/ürün işleri dondurulmuş kaynak ve bu alan dışında
sürebilir. GPU yürütücüsü yalnız Scientist'tir.

- Scientist kaynak commit: `0ae8677c6875ce10e48ef8e1e958d8f80c2e7de3`.
- Final imaj: `sha256:a14f144867d75a71f95861e48a3a075eecca445b73ccd541003a35c4e82bc3c1`.
- Harness: `b9d3890b35f341ac5ffe47f12a31d61934977b882497b0399e196dc86687ed42`.
- Yeni suite: `mode-agent-de75c37b05ca73430995a6c2639349877aa68b782cfdb007`.
- Registry: `99dec1c37aa04bfaafccc59bb1a0619675ba0796df2c2a478b727f6f140f9083`.
- Beklenen capability: `d01d4a114c61f4f8673b7165acd3787f8cfae0b1709a9bc208e3ef1ca3f27791`.

[İmaj kanıtı](review-evidence/current042-image-parity.json): varsayılan
kısıtlı kullanıcıyla 197 kaynak dosyası birebir eşleşir; CPU importları
exit0. Offline build, ağsız/pull olmadan yapılmıştır. Özel kaynak izinlerini
aynen kopyalayan ara imaj reddedildi; bu imajda GPU koşusu yapılmadı.
Yalnız yazılım kopyasının izinleri normalize edildi, repo ve özel kayıt
izinleri değişmedi.

Native paket hâlâ disabled; yeni APIv4 principal/generation üretildi,
GPU kabulü başlatılmadı. APIv2 ve sahip olunan eski clone
kapatıldı; eski araştırma ve scheduler kayıtları değiştirilmedi. Eski
başarılı hello araştırma raporu değildir. Final imajı bağlayan kalite kapısı tamamlandı: 2854pass/7skip/149deselected;
yedi komut exit0. Receipt SHA-256: `a9c2c8293ea5b567ddd66c13107fe4ddd494da0e89baf0897b1c9fdb151cc9fd`.


Scientist ve AOS oturumları doğrudan mesajlaşıyor. Scientist yalnız kendi
kaynaklarını ve sahipliği doğrulanmış izole hazırlığı yönetir; AOS kendi
kaynak/config, adaptör ve typed Lab akışının sorumlusudur. Entegre GPU kabul
koşusunun tek yürütücüsü Scientist'tir. Bağımsız CPU/kaynak işleri ortak
runtime incelemesini beklemeden sürer.

AOS yanıtı: yeni sözleşme veya kaynak/config farkı yok; tracked diff SHA-256
`27385fc37e117a2a100a3d5deb1842f1c6d9cd6313857f5eecdaa93a241a1b78`
sabit. AOS kendi özel handoff kaydında aday incelemesini ve typed
`propose → approve → execute → report` beklentilerini tutuyor; yeni API
generation/capability kanıtını inceleyecek. Scientist mevcut AOS kaynak,
veritabanı veya süreçlerini değiştirmedi.

Mevcut ortak `swapp-gpu.slice` üzerinde RAM 16 GiB, swap 0, CPU %200 ve
128 task sınırları gerçek kernel değerleriyle doğrulandı. Bu işlem GPU
rezervasyonu, model yürütmesi veya release kanıtı değildir. Yeni izole API
18602 hazırlığının ilk denemesi Director'ın şema okuma yetkisi olmamasıyla
durdu; yetki genişletilmedi, sahip olunan clone PID0 olarak kapatıldı.
Yeni yardımcı schema sürümünü clone üzerinden okur ve belirsiz servis
açılışında sahiplik/retirement kanıtını korur. Bağımsız incelemenin ardından
yeni yardımcı gerçek **exit 0** ile izole API'yi başlattı. Üretim capability
GET yanıtı, yeni principal/generation ve dar Scorer RPC üzerinden sentetik
mode kaydı doğrulandı; eski koşular değişmedi. API'nin süre sınırı 3600
saniyedir; önerilen ortak deney bütçesi 1 deney / 900 saniye / 30000 model
tokenıdır. Bu hazırlık model, broker, GPU veya deney başlatmadı.

Canlı API generation/capability kanıtı AOS oturumuna doğrudan iletildi.
AOS PID/generation/cgroup eşliğini ve 1 deney / 900 saniye / 30000 token
bütçesini bağımsız doğruladığını bildirdi. Tam native yapılandırma paketi
incelemesi ve native kabul koşusu açık kalır. Özel kanıt SHA-256'ları:

- API hazırlığı: `c46e1dfc6e7eb460334d2de1ce008177afd829e10183445268b09a0de78d5461`.
- API generation/capability: `f84c1c38846b9fb8a32238501a43b83a04acfeed1d3f612c76c9e809f9ace991`.
- Scorer mode kaydı: `688b5927ce6d4621b0f78eb534a8a075924d22524f9630d4cb35bf1bb3a0df84`.

Token, DSN ve özel runtime dosyaları yayımlanmaz. Sentetik kaynak
bağlaması gerçek endüstriyel ölçüm veya GPU kabulü olarak sunulmaz.

### Canlı API'ye bağlı tamamlanmış paket

Tamamlanmış disabled paket hazırlığı exit0: 135 kaynak/config pini
(2.115.961 bayt), gerçek API generation, principal kimliği ve yenilenmiş
clone bağlantısı üretim DTO'larıyla doğrulandı. AOS aynı paketi bağımsız
inceleyerek eşliği doğruladı. Ayrı promotion kopyası yalnız `enabled`,
onay bayrakları ve bağlı dosya pointer/hash değerlerini değiştirir;
özgün disabled kanıtlar korunur. Üretim `JointLabCapability` oluşturma ve
current-rights denetimi gerçek exit0 ile geçti (0,027 saniye).

Bu aşamada broker/AOS native servisleri ve modeller başlatılmadı. Yürütücü
masaüstü manifest/source/imaj pinlerini, tek konsol açılış deadline'ını
ve AOS cgroup dışında çalışan Docker konteynerinin ayrı cleanup kanıtını
sağlamalıdır. Promotion hash eşliği ve güncel API süresi yürütmede yeniden
kontrol edilir; GPU edinimi yalnız mevcut canonical scheduler üzerinden olur.

Aşağıdaki tarihli bölümler geçmiş incelemeleri anlatır; eski pinler ve
kapanış izinleri yeni inference yetkisi olarak kullanılmaz.

## Native devam için güncel beklenti — 2026-10-01

### Doğrudan oturum koordinasyonu: belirsiz, admission almamış istek

Güncel engel `3ccf93764f4c4f389864cc2edbdbcbfe` isteğinin AOS'ta pending
kalmasıdır. Önceki source52/63 gözlemleri tarihsel referanstır; gerçek kaynak
pinleri yeniden doğrulanmadan native işi açmaz.

Oturumlar doğrudan haberleşerek `no-admission-observation.v1` anlamsal
sözleşmesinde uzlaştı. Scientist şema/doğrulayıcı, canonical observational
tombstone ve mevcut admission transaction'daki geç kabul engelini; AOS kendi
tüketicisi, migration ve yeni source profilini yönetir. İki taraf CPU
uygulamalarını bağımsız ilerletir ve son şema hash'ini/gerçek test çıkışlarını
paylaşır. Sözleşme ve kapanış şartları:
[güncel ortak öneri](108-no-admission-observation-proposal.md).

Özgün istek/kayıt silinmez veya yeni runtime ile atlanmaz. Güncel lease ve
stopped/PAUSED generation yalnız cleanup yetkisi taşır. Şema eşliği, boş GPU
veya kapanmış süreç gözlemi tek başına terminal/release kanıtı değildir.
Yeni gerçek GPU koşusu ancak iki taraf da bağımsız gerçek kapanışı kabul
ettikten sonra Scientist tarafından yürütülür. AOS kaynakları bu oturum için
salt okunurdur; mevcut kullanıcı süreçlerine müdahale edilmez.

[Source52 ve durable resolution gereksinimi](91-aos-native-resolution-coordination.md):
güncel AOS v3 source52 exact snapshot uyumu geçti. Mevcut source'da original
inference intent'i append-only çözen API yok; ACK/receipt_recorded sonraki native
turu açmıyor. AOS oturumunun original identity + current rights + bağımsız
physical proof'a bağlı durable resolution ve buna bağlı admission gate sağlaması
gerekiyor. Scientist tarafı mevcut v3 authenticated budget/evidence export'unu
sağlıyor; intent silme/session reset/gate bypass uygulanmaz. AOS dosyası/süreci
bu oturumda değiştirilmedi; GPU testini tek Scientist koordinatörü yürütecek.

## Güncel Scientist bağlantısı — 2026-10-01

[Review edilmiş source snapshot](89-aos-reviewed-source-snapshot.md) ve
[authenticated retained-budget v3 candidate](90-aos-authenticated-retained-budget.md)
eski izole patch gözlemlerinden daha günceldir. Mevcut socket/target capability/
reconcile kotası üzerinde aynı transaction'dan evidence + bağımsız özgün budget
witness taşınır; v2 default korunur. Tam v3 schema canonical SHA
`cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070`.
Bu kaynak teslimidir; AOS v3 adoption ve production provider/proof/resolution
henüz kabul edilmedi. Gerçek AOS paralel olarak yeni `release_proof_candidate_v1`
48 kaynak profilini istedi; eski journal44 pinleri güncel değişiklikle ret veriyor.
Sonraki AOS v3 codec/migration0025 sürümü `retained_evidence_candidate_v3`
52 kaynak profili istiyor; full v3 schema eşliği salt okunur doğrulandı. Yeni52
kaynak review'ü ve exact config, tek scheduler rezervasyonu sonraki kapıdır.
GPU kabulünü yalnız Scientist oturumu koordine eder. AOS dosyası/süreci değişmedi;
push/merge/deploy yapılmadı.

## Güncel izole rebase — 2026-09-29

Canlı AOS'tan 1082 public dosya, 11.185.537 baytlık yeni kaynak kopyası
alındı; kopyalama boyunca kaynaklar sabit ve byte eşliği doğrulandı.
Snapshot yolundaki `20260927` eski sürücünün adıdır; gerçek kayıt tarihi
2026-09-29'dur. İki mevcut manifest farkı (`check_capabilities.py` ve testi)
korundu; yalnız ayrı merged kopyanın manifesti yeniden üretildi.

Güncel retention/readiness/pin davranışı korunarak typed Lab araçları,
ayrı dış iş kuyruğu, opt-in ortak GPU broker, bootstrap ve beklemeler
sonrası session/lease/generation kontrolleri 31 dosyalık yamada uzlaştırıldı.
Güncel migration zinciri 0017 ile biter; yeni 0018/0019 kopyada çakışmaz.
**Session 37161 / exit 0:** 109 hedefli test geçti, gerçek pinned Chromium
gerektiren bir test atlandı; paket kapısı **4360 kontrol / exit 0**.
Yama check/apply exit 0 ve ayrı temiz kopyada tam byte eşliği doğrulandı.

[Güncel kaynak kaydı](review-evidence/aos-current-rebase-preparation-0101ef6c806c.json),
[izole teslim](review-evidence/aos-current-rebase-20260929-receipt.json),
[güncel kaynağa bağlı yama](review-evidence/aos-current-lab-broker-20260929.patch).
Canlı AOS'a uygulanmadı. GPU/model/gerçek birlikte çalışma başlatılmadı;
bekleyen GPU koordinasyon yanıtı ve gerçek birlikte ilerleme kabulü korunur.
İlk git metadata envanteri preflight hatası özel kanıtta tutulur;
o denemede test çalışmadı. Bu teslim CPU kaynak/paket doğrulamasıdır.

## Salt okunur yeniden kontrol — 2026-09-27

On iki entegrasyon temas dosyasının **yedisi**, 2026-09-24 18:50 UTC
görüntüsünden farklı. İsimle yapılan sınırlı aramada `src/aos`, `services`
ve `scripts/serve_desktop.py` altında `ai_scientist`, `lab.start/status`
ve ortak GPU adapter adları bulunmadı; bu, farklı adla uygulanmış bir
bağlantıyı dışlamaz. Koordinasyon dosyası adı araması yalnız mevcut devir
notunu buldu; yanıt veya yeni izin doğrulanmadı.

AOS dizininde erişilebilir Git HEAD yok; gelecekteki uzlaştırma dosya
hash'leriyle yapılmalı. Eski izole yamalar güncel canlı kaynağa doğrudan
uygulanamaz. Bu kontrolde AOS dosyası, servis, model veya DB değiştirilmedi.
Kaynak hash'leri, komutlar ve gerçek çıkış kodları:
[salt okunur kontrol kaydı](review-evidence/aos-integration-readonly-20260927.json).
Bekleyen GPU koordinasyonu sürerken Lab'ın CPU geliştirmesi devam ediyor.

### Güncel kaynak kopyası ve yama kontrolü

`data/runtime/aos-coexistence/source-rebase-20260927-0a9e16023f95`
altına 937 public kaynak dosyası (9.586.870 bayt) kopyalandı. Kaynaklar ve
dosya envanteri kopyalama boyunca sabit kaldı; kopya byte eşliği doğrulandı.
928 dosyalık mevcut manifestin beş hash farkı ve manifest dışındaki dokuz
public dosya ayrı kaydedildi; bu bir paket doğrulama sonucu değildir.

Tarihsel `aos-lab-gpu-broker.patch` için **uygulamadan yapılan
`git apply --check` exit 1** verdi. Beş mevcut dosyada hunk eşleşmiyor:
`scripts/serve_desktop.py`, `services/decider/worker.py`,
`src/aos/desktop_console.py`, `src/aos/reusable_decider.py` ve
`src/aos/supervisor.py`. Yamanın yeni dosya olarak eklediği
`MANIFEST.sha256` ile `services/laya/worker.py` de güncel kopyada zaten var.
Kontrol kopyadaki kaynakları değiştirmedi. Eski manifesti veya güncel
dosyaları silerek bu farklar giderilmiş sayılmaz; entegrasyon güncel
kaynak davranışı korunarak yeniden hazırlanmalı.

[Kaynak ve kontrol kaydı](review-evidence/aos-current-rebase-preparation-0a9e16023f95.json)
937 dosyanın hash'ini, komutu ve gerçek hata çıktısını içerir;
[kopyalama sürücüsü](review-evidence/aos-current-rebase-snapshot-driver.py)
aynı SHA ile arşivlendi. Hazırlık sürücüsünün exit 0 sonucu yalnız kopya ve
kontrol kaydının üretildiğini gösterir; yamanın başarıyla uygulandığı
anlamına gelmez. Canlı AOS kaynağı veya çalışma durumu değiştirilmedi,
servis/model/DB başlatılmadı. Gerçek birlikte çalışma kabulü açıktır.

Astra/high bağımsız kaynak incelemesinde 937 hash'i yeniden doğruladı.
Mevcut `services/laya/worker.py`, eski yamadaki eklenen dosyayla byte eş;
yeniden eklenmesine gerek yok. Manifest hunk'ı yerine birleşik test
kopyasının manifesti son kaynaklar üzerinden üretilecek. Güncel kaynakta
ana migration zinciri `0017` ile bitiyor; yamanın `0018/0019` adları teslim
öncesinde paralel geliştirmeyle tekrar kontrol edilmeli.

Uzlaştırmada korunacak güncel davranışlar:

- `serve_desktop.py` GPU retention seçenekleri ve mevcut lifespan:
  ortak broker opt-in olduğunda CPU prewarm/GPU retention çakışması
  reddedilmeli; mevcut kapanış, scheduler ve pin prewarm adımları korunmalı.
- Decider worker'ın `prepare_idle_gpu/admit_job_gpu` kontrolleri ve
  `ReusableDeciderEngine` idle/prewarm teardown yolları korunmalı;
  broker'a özel readiness bunları atlamamalı.
- `BonsaiSupervisor.plan` ortak broker yolu, güncel `verify_pins_for_call`
  kontrolünden sonra gelmeli; pin cache/prewarm ve çağrı metrikleri korunmalı.
- Desktop kontrolündeki restart/planning/sequence kilitleri korunmalı.
  Lab handler'ları uzun araştırmayı foreground slotuna bağlamamalı;
  beklemelerden sonra session/lease/generation yeniden doğrulanmalı.
  Restart/quiesce sırasında çözülmemiş Lab işleri yeni admission'ı kapatmalı.
- Tam GPU yaması typed/API/storage değişikliklerini zaten içeriyor;
  eski typed-policy yaması tekrar uygulanmamalı. Sonraki lifecycle ve
  control-fence yamalarının davranışları güncel kaynağa ayrıca taşınmalı.

## Son koordinasyon gereksinimi — 2026-09-25

0.25 gerçek Qwen öneri denemesi sırasında canlı AOS'un
`models/bonsai-runtime-prism-b10709-9a9394a/llama-prism-b10709-9a9394a/llama-server`
süreci ortak kuyruğa katılmadan GPU kullandı. Süreç `session-28.scope`
altındaydı; Lab gözlemcisi yalnız kendi test süreçlerini kapattı.
Session 54140 / exit 1 sonucunda geçerli öneri veya birlikte çalışma
kabulü oluşmadı. Kanıt: `review-evidence/local-qwen-provider-025-outcome.json`.

Gerçek kabul denemelerini ilerletmek için paralel AOS oturumunun model
başlatma yolları ortak broker'a bağlanmalı veya ayrı, kısa bir GPU test
aralığı koordine edilmeli. Bu seçim kullanıcıya iletildi; henüz yanıt
kaydedilmedi. Canlı AOS dosyaları ve servisleri değiştirilmedi. Ayrı test
kopyasının lifecycle/control yamaları hazır; desktop ve araştırma
birlikte test sürücüsünün ikinci sürümü hazırlanıyor.

Lab broker doğrulayıcısının dar ek yaması, sonradan oluşan canonical
`swapp-ai-scientist-director-dispatch-<run UUID>.service` üst sürecini
yalnız Lab sahibi için kabul eder; PID/başlangıç/boot/InvocationID/cgroup
eşliği zorunludur. Sabit owner çözümlemesi ve AOS ad alanı korunur.
Hedefli 27 test geçti; yama ve kaynak bağları
`review-evidence/lab-runbound-lab-principal-025-main-source.json` içindedir.
Bu CPU doğrulaması gerçek AOS araştırma kabulü değildir.

Yeni izole teslim (2026-09-25): `review-evidence/aos-lab-optin-lifecycle.patch`
normal desktop bootstrap'a açık Lab seçenekleri, model çağrısı sonrası güncel
yetki kontrolü, eşzamanlı aynı-key start birleştirmesi ve restart/takeover
drain davranışı ekler. Yama frozen `c33325349e856edf52d1ab37da394388eb0607c2`
kopyası içindir; canlı AOS'a doğrudan uygulanacak yama değildir.
Dokuz dosyanın base/head hash eşliği ve bağımsız `git apply --check` geçti.
24 hedefli test ve 4100 paket kontrolü exit 0; geniş tarayıcı testinde
host'a özgü `browser-manifest.json` eksik olduğundan 12 test başarısızdır.
Tam Ruff mevcut 206 tanıyla exit 1; değişen satırlarda tanı bulunmadı.
Kanıtlar `aos-lab-optin-lifecycle-review.json` ve
`aos-lab-optin-lifecycle-root-review.json`. Bu dilimde model veya GPU
çalıştırılmadı; gerçek araştırma/desktop birlikte kabulü açıktır.

Ardışık ek yama `aos-lab-optin-control-fence.patch`, control policy await'i
sonrasında session/binding/typed state'i yeniden doğrular; stop için güncel
HUMAN lease/generation/runtime de eşleşmelidir. Kısa deadline bu kontrolden
sonra başlar. Status/report deterministik host policy kullanır, polling
LLM açmaz. İki yamanın sırayla uygulanması ve son üç dosyanın byte eşliği
root tarafından doğrulandı. 27 hedefli + restart API testi ve 4100 paket
kontrolü exit 0; gerçek model/desktop kabulü açık. Kanıt
`aos-lab-optin-control-fence-review.json` ve `...-root-review.json`.

Güncel sonuç (2026-09-25, Lab 0.20): izole kopyada gerçek
**Qwen S1 → Decider → Qwen S2 → Bonsai** çağrılarının dördü de geçti.
Session 52269 / exit 0, 12/12; 211,187 sn. GPU zirve 12754 MiB ve son
62 MiB; en düşük kullanılabilir RAM 18,4 GiB. Kaynaklar sabit ve bütün
test süreçleri kapandı. Aritmetik/sentetik girdiler kullanıldı; tam
araştırma ve desktop kabulü açık. Canlı AOS değiştirilmedi.
Kanıt: `aos-qwen-020-broker-model-outcome.json`.

Sonraki Lab 0.21 tek S2 araştırma probe'unda, ortak kuyruğa katılmayan
`/home/cachyos/.venv/bin/python` süreci `session-26.scope` içinde 1180 MiB
GPU belleğiyle gözlendi (session 20177 / exit 1). Lab yalnız kendi test
birimlerini durdurdu; dış süreç korunup AOS'a atfedilmedi. Kaynak dosyalar
değişmedi. Paralel AOS oturumu model denemesi yapıyorsa gerçek birlikte
kullanım için model yükleme/çıkarım yollarının da opt-in ortak broker'ı
kullanması gerekiyor. Sadece Lab'ın sıraya katılması yeterli değil.
Kanıt: `local-qwen-compact-021-s2-review.json`.

Önceki durum (2026-09-25): izole güncel AOS kopyasında gerçek Decider ve Bonsai
recovery çağrıları ortak GPU aracısından geçti; iki model süreci çağrı
sonunda kapandı (8/8, session 38880 / exit 0, GPU 62→8816→62 MiB).
Ek dosyalar `services/decider/broker_worker.py` ve
`services/bonsai/broker_worker.py`; mevcut `services/decider/worker.py`
için GPU hazırlık sınırı ve istemci metrikleri değişikliği de gerekiyor.
Bu kaynaklar yalnız test kopyasında. Canlı AOS değişmedi.
Sonraki birleşik tanı denemesinde gerçek sıra Lab → AOS → Lab → AOS oldu;
S1/Decider/Bonsai geçti, S2 512 token sınırına takıldı (10/11 kontrol,
session 74499 / exit 1). GPU süreçleri temizlendi, son bellek 62 MiB.
Bu nedenle tam Qwen araştırması ve gerçek desktop kabulü açık kalır.
Lab broker'ı 0.19.0 kalıcı paketine alındı; 231 testli tam kalite kapısı ve
imaj byte eşliği geçti. `20-aos-broker-model-review.md` kapsamı kaydeder.

Güncel teslim `review-evidence/aos-lab-gpu-broker.patch`: kayıtlı baseline
manifest `fb0c38f2…` üzerine 24 dosyalık tam yama; eski yamalarla zincirlenmez.
Bağımsız uygulama/byte eşliği, 57 hedefli test (9 tarayıcı testi atlandı)
ve 4100 paket kontrolü geçti. Bu yama mevcut canlı dosyalara uygulanmadan
önce paralel değişikliklerle yeniden uzlaştırılmalıdır.

## AOS tarafında gereken dar değişiklikler

Alan | Muhtemel dosyalar | Gerekli davranış
--- | --- | ---
Ortak GPU kaynağı | `src/aos/decision.py`, `src/aos/reusable_decider.py`, `src/aos/supervisor.py`; gerekirse `services/decider/worker.py` | Opt-in shared lease adapter. CUDA/model yüklemeden önce al, kendi GPU süreci drain/unload olmadan bırakma. CPU prewarm ayrı RAM rezerviyle devam edebilir. Reusable Decider normal yanıtta VRAM tutuyor; sadece mutex yeterli değil.
Vision kapsamı | `src/aos/vision.py` | `BonsaiVisionSupervisor` base supervisor yürütmesini kullanıyor; ortak hook'un vision ve recovery'yi kapsadığı doğrulanmalı.
Typed uzman servis | `src/aos/contracts.py`, yeni `src/aos/ai_scientist*.py`, `src/aos/computer.py` | Sınırlı `ai_scientist` task/`lab.start`, `lab.status`, `lab.stop`, `lab.report` eylemleri; arbitrary URL veya shell yok. State, policy, caller identity, bütçe, idempotency ve doğrulama korunur.
Arka plan iş takibi | `src/aos/desktop_tasks.py`, append-only DB migration/yeni external-job repository | Lab run id'sini durable kaydet; uzun Lab koşusu foreground DesktopScheduler slotunu veya input lease'i işgal etmesin. AOS aynı anda başka yetkili kullanıcı görevini yürütebilsin. Start ACK araştırma başarısı sayılmaz; terminal rapor ayrı doğrulanır.
İsteğe bağlı UI/katalog | `src/aos/desktop_console.py`, ilgili task catalog/schemas | Önce API/typed yürütme doğrulanır; kullanıcıya başlatılmış/devam ediyor/tamamlandı durumları açık ayrılır. Türkçe serbest görev normalizer'ı ve birleşik planlar, mevcut dar katalog kanıtını kaybetmeden sonra genişletilir.

## Sınır ve birlikte doğrulama

- Bu projenin Lab API/Qwen/Scorer/Postgres bağımlılıkları AOS venv'ine kurulmaz. AOS'a taşınacak adapter hafif ve transport sözleşmeli olmalı.
- AOS SQLite ve Lab Postgres ortak tablo yazmaz. Cross-reference: AOS task/run/action ↔ Lab run id + request digest + rapor hash.
- Foreground görev bitişi ile bağımsız araştırma işi bitişi karıştırılmaz. Başlatma eylemi, kalıcı Lab handle ve bütçe doğrulamasıyla doğrulanabilir; araştırma işi ancak terminal raporla tamamlanır. Geri plan araştırması kendi sınırlı yetkisine sahiptir; sonraki foreground göreve eski approval taşınmaz. Targeted stop davranışı açık olmalı.
- Lab'ın vLLM'i GPU'yu koşu boyunca tekeline alamaz. AOS interaktif talebi ve Lab talebi sonlu inference dilimleri, unload ve adil kuyruk üzerinden ilerler. Mevcut AOS canlı oturumu koordinasyon olmadan değiştirilmez/kapatılmaz.
- İlk test ayrı opt-in AOS instance'ı ve ayrı DB/port/workspace ile; gerçek modellerde resource transfer, kontrol API yanıtı ve her iki işin ilerlemesi ölçülür. Fixture testi GPU kapasitesi kabulü değildir.
- Mevcut AOS kaynakları paralel değişiyor. [İncelenen dosyaların hash'leri](review-evidence/aos-source-baseline.json) başlangıç referansıdır, kilit değildir. Yama uygulamadan önce dosyalar yeniden okunmalı; hash farklıysa patch yeniden tabanlanmalı. Kullanıcının/paralel oturumun değişiklikleri geri alınmaz.

Henüz AOS dosyası değiştirilmedi ve mevcut yönetilen AOS oturumu yeniden başlatılmadı. İlgili oturumla doğrudan mesaj kanalı mevcut değil; bu dosya koordinasyon için somut devir metnidir.

Güncelleme 2026-09-24: Kullanıcının yinelenen bilgilendirme talebi üzerine AOS köküne yeni `AI_SCIENTIST_COORDINATION.md` devir notu eklendi. Mevcut AOS dosyaları ve çalışan servisler değiştirilmedi. Diğer oturumun notu okuduğu henüz doğrulanmış değil.

Bu not sonrasında AOS package validation exit 1 verdi: `Manifest hash: tests/test_web_https_relay.py`. İlgili mevcut test ve manifest değiştirilmedi. [Koordinasyon kontrol kaydı](review-evidence/aos-coordination-check.json), kontrolün anlık durumunu ve yeni notun hash'ini tutar; gerçek AOS entegrasyon kabulü sayılmaz.

2026-09-24 09:49 UTC salt okunur yeniden incelemesinde `src/aos/desktop_tasks.py` ve `src/aos/desktop_console.py` başlangıç hash'lerinden farklıydı; diğer on temas dosyası aynıydı. [Kaynak değişim kaydı](review-evidence/aos-source-drift-review.json). Bu durum paralel çalışma olduğunu doğrular; önceki kaynak görüntüsüne dayalı yama doğrudan uygulanmaz. Lab tarafındaki hazırlık AOS dosyalarını değiştirmedi; entegrasyon diliminde güncel dosyalar yeniden okunur.

## Ayrı entegrasyon test kopyası

2026-09-24'te `data/runtime/aos-coexistence/source` altında AOS kaynak kopyası ve kardeş `.venv` altında bağımsız Python 3.12.13 ortamı hazırlandı. Canlı AOS dosyası, DB'si, modeli, token'ı veya oturumu kopyalanmadı/değiştirilmedi; yalnız paket kaynakları alındı. [Kaynak hash kaydı](review-evidence/aos-test-copy-source.json) 663 manifest dosyasını (~5.95 MB) ve manifestte henüz bulunmayan dört public kaynak dosyasını listeler. İlk kopyalama sırasında kaynak değişmedi. Bu görüntü paralel çalışmanın sonradan yaptığı değişiklikleri otomatik içermez.

[Ortam ve temel kontrol kaydı](review-evidence/aos-test-copy-check.json): frozen AOS `uv.lock` ile desktop/dataset/browser Python bağımlılıkları, ayrı validation bağımlılıkları; 29 paket `uv pip check` exit 0. `aos.contracts.REPO_ROOT` test kopyasını gösterir. Reusable Decider ve DesktopScheduler mevcut CPU fixture testleri 35/35 geçti. Tarayıcı, AOS servisi ve GPU modeli başlatılmadı.

İlk package validation, henüz manifestte olmayan retention şeması eksikken hata verdi. Dört eksik public dosya eklendikten sonra gerçek schema/fixture doğrulaması ilerledi; kaynak manifestteki güncel README hash uyuşmazlığı nedeniyle package validation yine exit 1. Kaynak kopyasında 11 manifest hash farkı kaydedildi; orijinal manifest korundu. Bunlar entegrasyon yaması öncesi temel durumdur ve başarı olarak sunulmaz. Yama uygulanınca yalnız test kopyasının yeni paket manifesti ve hedefli kontrolleri üretilir; canlı AOS'a aktarım ayrıca güncel kaynakla uzlaştırılır.

## Güncel kaynak karşılaştırması

2026-09-24 18:50 UTC tarihli salt okunur incelemede, on iki entegrasyon temas dosyasının dördü önceki incelemeden ve ayrı test kopyasından farklıydı: `contracts.py`, `computer.py`, `desktop_tasks.py`, `desktop_console.py`. Exact kontrol zamanı ve SHA değerleri [güncel kaynak kaydında](review-evidence/aos-integration-inputs-current.json) bulunur. Bu, tüm AOS paketinin fark sayısı değildir; yalnız bu on iki dosyanın karşılaştırmasıdır.

Güncel typed görev/araç kataloğu hâlâ Lab eylemlerini içermiyor; `src/aos` ve `schemas` aramasında `ai_scientist`, `lab.start`, `gpu_scheduler` veya ortak GPU lease bağlantısı bulunmadı. Entegrasyonun tamamlandığına dair kanıt yoktur. Paralel kaynaklar, canlı süreçler, veritabanları ve mevcut test kopyası bu kontrolde değiştirilmedi. İzole entegrasyon yaması hazırlanırken bu dört dosya güncel sürüme göre yeniden incelenmelidir; eski kopyanın üzerine kör yama uygulanmaz.

## 2026-09-25 — izole typed yama hazır

`source-typed` test kopyasında `ai_scientist` State/Action, typed `lab.*`
policy, kalıcı dış iş kimliği, açık rebind ve takeover stop/hata kaydı eklendi.
Başlatma karar motoru enjekte edilir; durum/rapor kontrolü deterministik host
policy kullanır. Arka plan Lab işi foreground DesktopScheduler'a eklenmez.

Teslim: `review-evidence/aos-lab-typed-policy.patch` ve dosya hash'lerini içeren
aynı adlı JSON. Yama, tam olarak kayıtlı **source-current test kopyasını**
hedefler; onun manifest'i eski mapping yamasının teslim manifest'inden farklıdır.
Yamalar kör zincirlenmez ve canlı AOS'a doğrudan uygulanmış sayılmaz.
On bir dosyanın geçici Git dizininde byte eşliği doğrulandı. Güncel kopyada
32 hedefli test, 3842 paket kontrolü ve Lab tarafında 176 testli tam gate geçti.

Entegrasyonun mevcut dosya temasları: `computer.py`, `contracts.py`,
`dataset_audit.py`, `desktop_console.py`, `lab_external.py`, `storage.py`,
dataset-audit/Lab testleri ve manifest. Yeni migration
`0019_lab_external_job_rebinding.sql` test kopyasına aittir; paralel AOS
migration numaralarıyla birleştirmeden önce uzlaştırılmalıdır. Eksik public
`services/laya/worker.py` yalnız test kopyasının paketine eklendi.

Normal `serve_desktop.py` opt-in bootstrap'ı, gerçek model/GPU hook'ları,
gerçek in-flight stop/restart ve takeover iş sayısı sınırı açık. Bu kaynaklar
canlı oturumda değiştirilmedi. Güncel inceleme ve kanıt kapsamı:
`16-aos-typed-policy-review.md`.
# GPU sıra paylaşımı güncellemesi — 2026-09-25

Lab 0.15.0, sabit `aos`/`lab` hizmet kimlikleri ve dönüşümlü kuyruk çekirdeğini
uygular. İki gerçek systemd CPU servisiyle 20 dönüşümlü işlem ve 11 kontrol
geçti; gerçek model veya canlı AOS kullanılmadı. Ayrıntı ve sınırlar
`17-gpu-arbitration-review.md` içindedir.

Sonraki dar AOS entegrasyonu `ReusableDeciderEngine`, `BonsaiSupervisor` ve
Bonsai'ye yönlenen vision çağrılarının CUDA yükleme öncesinde bu kuyruğa
katılmasıdır. Normal opt-in bootstrap, fixed principal/unit yapılandırmasını
kurmalı; model veya kullanıcı isteği hizmet kimliğini seçememelidir. Lab
status/report sorguları GPU modeli çağırmaz. Kaynak bırakma için gerçek owned
model sürecinin durması ve VRAM gözlemi ayrıca gerekir.

Bu değişiklikler `data/runtime/aos-coexistence/source-gpu` adlı yeni public
kaynak test kopyasında hazırlanır. Önceki `source-current` ve `source-typed`
kopyaları kabul kanıtları için dondurulmuştur. Canlı `/home/cachyos/aos` üzerinde
bu dilimde değişiklik veya yeniden başlatma yapılmadı; oraya uygulama öncesinde
mevcut paralel geliştirmeyle dosya/migration çakışmaları yeniden uzlaştırılır.

Host'ta kısa süreli dış Python GPU kullanımı gözlendi; kendiliğinden sona erdi.
Tek bir boş-GPU snapshot'ı rezervasyon sayılmaz. Native model denemelerinde
başlangıç ve çalışma sırasındaki tüketici kontrolü, toplam kaynak bütçeleri ve
yalnız sahipli süreçlerin durdurulması zorunludur.

## 2026-09-25 — gerçek GPU denemesinde dış tüketici gözlemi

Gerçek Qwen S2 doctor denemesinin sonunda testin dışındaki `session-19.scope`
içinde bir Python GPU tüketicisi ortaya çıktı. AI Scientist gözlemcisi yalnız
kendi test modelini ve supervisor'ını durdurdu; dış süreç GPU'da kalmaya devam
etti. Kaynak uygulamanın AOS olduğu doğrulanmadığından bu olay AOS'a
atfedilmez. Kanıt: `review-evidence/native-qwen-s2-sampling-profile-review.json`.

Ortak GPU kuyruğuna katılmayan bir süreç varken boş bellek kontrolü tek başına
rezervasyon sağlamıyor. AOS tarafında Decider/Bonsai/vision model yükleme,
çağrı ve unload yollarının ortak yöneticiyi kullanması hâlâ gereken dar
entegrasyondur. Hazırlık izole `source-gpu` kopyasında yapılacak; canlı
kaynakları değiştirmeden önce paralel oturumla bu dosya üzerinden uzlaştırma
gerekiyor. Bu olayda canlı AOS dosyası veya süreci değiştirilmedi. CPU kalite
kontrolleri sürerken yeni GPU denemesi başlatılmadı.

## 2026-09-25 — model tanımları ve güncel kaynak farkı

Salt okunur `review-evidence/aos-gpu-model-metadata-review.json` incelemesi,
mevcut Bonsai manifestinin `prism-ml/Ternary-Bonsai-2-27B-gguf`, PQ2_0 ağırlık
ve Q8_0 vision projector kullandığını kaydeder. Manifestte context 16384,
çıktı 512 ve parallel 1 yer alır. Decider'ın yedi dosyası stat toplamıyla
3.78 GB, Bonsai'nin iki dosyası 7.84 GB'dır. Bunlar dosya boyutlarıdır;
RAM/VRAM kapasitesi veya ağırlık hash doğrulaması değildir. Model, cache,
credential veya özel DB kopyalanmadı; hiçbir AOS/model servisi başlatılmadı.

İncelenen **altı GPU temas dosyasından ikisi** `source-gpu` test kopyasından
farklıdır: `reusable_decider.py` artık CPU prewarm idle sınırını 600 saniyeye
kadar kabul ediyor; `serve_desktop.py` buna ilişkin CLI seçeneği ile yeni form
öğrenme seçenekleri içeriyor. Bu değişiklik CPU hazırlığının ömrüne ilişkindir;
GPU'nun 600 saniye tutulduğuna dair ölçüm değildir. Ortak GPU hook'u
hazırlanırken paralel çalışmanın bu değişiklikleri korunup uzlaştırılmalıdır.
Kaynakların inceleme sırasında değişmediği hash ile kontrol edildi. Canlı
dosyalar ve çalışan oturumlar bu incelemede değiştirilmedi.

## 2026-09-25 — izole GPU istemcisi, protokol taslağı

`source-gpu` içinde `aos/gpu_turn.py`, Decider/Bonsai/vision çağrı bağlantıları
ve `serve_desktop.py --shared-gpu-turns` taslağı hazırlandı. Önceki kopyalanmış
`shared_gpu.py`/`gpu_scheduler.py` yolları bu izole kopyadan kaldırıldı.
Yeni istemci sabit profilli Unix soketi isteği gönderir; kuyruk, model başlatma
ve GPU'nun boşaldığını doğrulama Lab hizmetine ait olacak. Gerçek broker ve
Decider/Bonsai için sabit model çalıştırıcıları henüz uygulanmadı; mevcut
Qwen çalıştırıcısı AOS modellerini çalıştırıyor sayılmaz.

İlk taslakta `SO_PEERCRED` PID/UID alan sırası ve `systemctl --user` ortamı
yanlıştı; gerçek yerel negatif kanıt `aos-gpu-client-initial-defects.json`.
Düzeltmeden sonra `review_aos_gpu_peer.py` **6/6, exit 0**: gerçek kısa
systemd CPU servisi/Unix soketi, doğru servis kimliğiyle istek/yanıt, yanlış
PID/UID reddi ve yalnız sahipli test servisinin temizliği doğrulandı.
`aos-gpu-client-actual-peer-review.json` kaynak hash'ini ve komutları saklar.
Dönen inference/model generation alanları açık fixture'dır; model, GPU
paylaşımı veya birlikte çalışma kabulü değildir.

Bu taslak canlı AOS'a uygulanmadı. `source-gpu` hâlâ önceki kaynak görüntüsünü
temel alır; canlı 600 saniyelik CPU prewarm ve yeni form seçenekleriyle
uzlaştırılmadan dağıtım yaması olarak kullanılamaz. İptal/yeniden bağlanma,
durable istek kimliği, gerçek model süreç sahipliği ve bağımsız drain
doğrulaması da tamamlanmalıdır. `source-current`/`source-typed` korunuyor.

## 2026-09-25 — yerel model ve runtime dosyalarının hash doğrulaması

Bir sonraki salt okunur kontrol, manifestlerin gösterdiği gerçek dosyaları
SHA-256 ile doğruladı: Decider için **7 model + 4 kod dosyası**, Bonsai için
**2 model + 84 runtime dosyası**; toplam **97 dosyanın tamamı eşleşti**.
Dosya kimliği/boyutu/zaman damgaları ve manifestler okuma boyunca sabit kaldı.
Kanıt: `review-evidence/aos-local-model-pin-review.json` ve ayrı supervisor
exit kaydı `aos-local-model-pin-review.launch.json`; ikisi de exit 0.

Kontrol 256 MiB/1 CPU sınırında, düşük CPU ve disk önceliğinde 6,28 saniyede
tamamlandı. Dosyalar yalnız okunup hash'lendi; kod import edilmedi, model
yüklenmedi, cache kopyalanmadı, çalışan AOS değiştirilmedi. Bu doğrulama
manifest ile yerel byte eşliğini kanıtlar; gerçek inference, GPU belleği,
adil devir veya eşzamanlı ilerleme kabulü hâlâ açıktır. İzole entegrasyon
başlangıcında bu kimliklerin değişmediği yeniden kontrol edilmelidir.

## 2026-09-25 — 0.18 deneyinde yeniden dış GPU tüketicisi

Ortak kuyruğa katılmayan Python süreci (`session-19.scope`, 3894 MiB) ikinci
gerçek araştırma denemesini baseline aşamasında durdurdu. Kaynak uygulama
AOS olarak doğrulanmadı; dış süreç değiştirilmedi. `local-qwen-018-wire-review.json`
ve `local-qwen-018-wire-outcome.json` kanıtı korur. Lab model çağrısı başlamamıştı.

Paralel AOS çalışması için gereken entegrasyon aynı: Decider/Bonsai/vision
CUDA yüklemeleri ortak yöneticiden geçmeli; ayrı test oturumunda adil devir
ölçülmeli. Dış GPU süreçleri bu kuyruğa katılmadan iki uygulamanın birbirini
engellemeyeceği garantisi verilemez. Canlı AOS mevcut dosyaları ve süreçleri
değişmedi; dar yama izole `source-gpu` kopyasında geliştiriliyor.

## 2026-09-25 — güncel public AOS kaynağına izole rebase

Güncel canlı AOS'un public kaynak görüntüsü ile typed Lab/GPU değişiklikleri
ayrı `data/runtime/aos-coexistence/rebase-f16d3ced7e6d430eb9b2e11913dac720/merged`
kopyasında birleştirildi. Canlı dosyalar, süreçler, modeller ve DB değişmedi.
`source-current`, `source-typed` ve eski `source-gpu` korunuyor.

Üç çakışma çözüldü: `reusable_decider.py` CPU prewarm 600 saniye, worker
yeniden hazırlama ve EOF/bozuk JSON hata yollarını; `serve_desktop.py` idle
argümanı, form seçenekleri ve form metadata kapsamını; `desktop_console.py`
yeni form/restart-quiesce kodunu korur. Typed Lab araçları/ayrı iş kayıtları
ve opt-in GPU hook'ları da korunmuştur. Kaynak manifestinin gerisinde kalan
altı yeni public dosya ayrıca hash'lenip kopyalandı; özel runtime/veri yok.

**55 hedefli test / exit 0**, **4096 paket kontrolü / exit 0**. Gerçek
`serve_desktop.py --help` importu ve yeni/eski seçeneklerin görünmesi exit 0.
İlk yanlış interpreter yolu ve eksik public fixture gate hatası kanıtta
korunur; dependency kurulumu veya canlı source düzeltmesi yapılmadı.

Teslim `review-evidence/aos-gpu-rebased-source.patch`; kaynak/komut/sonuç
bağı `review-evidence/aos-gpu-rebase-review.json`. Yama captured live kaynak
görüntüsünde uygulanıp **20/20 değişen dosyada byte eşliği** verdi. Canlı
MANIFEST yamaya dahil edilmez; güncel kaynakla uzlaştırılınca yeniden üretilir.
Bu yama doğrudan canlı AOS'a uygulanmış veya gerçek GPU kabulü almış değildir.

Yeni `/api/restart/quiesce` yolunun detached Lab işleriyle stop/kurtarma bağı,
normal `serve_desktop` içinde typed Lab coordinator bootstrap'ı ve gerçek
Decider/Bonsai broker çalıştırıcısı açık kalır. GPU testinde kullanılacak
sonraki AOS kodu bu rebased kopyada devam edecektir.


## 2026-09-25 — gerçek broker kanıtı ve dış GPU tüketicisi

Son izole broker yaması 24 dosyadır; 57 hedefli test/4100 paket kontrolü
exit 0, patch uygulaması byte eşliğiyle doğrulandı. Kaynak ve sınırlar
`20-aos-broker-model-review.md` ve `aos-lab-gpu-broker-patch-review.json`
içindedir. Dört gerçek tanı çağrısı Qwen S1 → Decider → Qwen S2 → Bonsai
sırasında tamamlandı (session 52269 / exit 0); tam araştırma/desktop kabulü
henüz değildir. Canlı AOS'a kurulum yapılmadı.

Son tek S2 denemesinde ortak kuyruğa katılmayan dış Python GPU tüketicisi
model sonucu oluşmadan testi durdurdu (session 44215 / exit 1). Kaynağın
AOS olduğu doğrulanmadı; dış sürece müdahale edilmedi. Kanıt
`local-qwen-022-s2-syntax-diagnostic.json`. Paralel AOS çalışmasının GPU
çağrıları da aynı opt-in broker protokolüne katılmalıdır. Bu koordinasyon
olmadan bağımsız GPU denemelerini kör tekrarlamak birlikte çalışma kabulü
sağlamaz. Lab CPU/public veri/holdout işi devam eder; canlı AOS kaynakları
ve model ayarları paralel geliştirmeyle uzlaştırılmadan değiştirilmez.


## 2026-09-25 — normal başlatma ve model bekleme yarışı

Normal `serve_desktop.py` opt-in GPU broker'ını kuruyor ancak Lab dış iş
koordinatörünü henüz console'a bağlamıyor. `start()` model kararını
beklerken HUMAN lease değişirse eski yetkiyle Lab çağrısı yapılabildiği
izole SQLite/fixture testinde yeniden üretildi (exit 1); canlı AOS veya
gerçek model kullanılmadı. `23-provider-syntax-and-aos-lifecycle-review.md`
kanıtı ve sınırı açıklar. Luna yeni public test kopyasında `lab_external.py`,
`serve_desktop.py`, `desktop_console.py`, ilişkili test/docs/manifest üzerinde
çalışıyor. Karar sonrasında yetki kontrolü, normal bootstrap, restart/quiesce
ve en fazla sekiz aktif dış iş kapsam içindedir. Canlı AOS'a yazılmıyor;
paralel oturumun aynı dosyalardaki değişiklikleri aktarım öncesi uzlaştırılacak.

## 2026-09-25 — ortak veritabanı ve ayrı 0.27 çalışma kopyası

Lab 0.27, broker'ın sabit
`~/.local/state/swapp-gpu/arbiter.sqlite3` dosyasını kullanabilir. Broker,
her araştırma koşusunun canonical Director unit kimliğini de doğrular;
PID başlangıcı, boot, InvocationID ve cgroup kontrolleri korunur.
Kaynak commit'i `390a6c25e94e5da339457dd17e88749ed07c7795` ve kalite
bağı `review-evidence/parallel-integration-027-quality-gate-binding.json`.

`data/runtime/parallel-m0/public-execution-027` ayrı çalışma kopyasıdır.
Public v3 manifesti, research profili, private API principal ve rol bazlı
DB bağlantıları hazırlandı. Üretim kayıt defteriyle manifest/config
hash'leri ve modül kökleri doğrulandı; gerçek preflight exit kodu 0.
Model dosyaları ayrı salt okunur reflink kopyalarıdır; model hash kontrolü
çalıştırma öncesinde yine zorunludur. Hazırlık hiçbir API, broker veya
GPU modeli başlatmadı. Kanıt:
`review-evidence/private-public-execution-027-preflight-outcome.json`.

0.25 S2 denemesinde bağımsız GPU'ya giren süreç bu kez AOS'un
`llama-server` dosyasına ve `session-28.scope` cgroup'una bağlandı
(`review-evidence/local-qwen-provider-025-outcome.json`). Yalnız Lab'ın
kendi model süreçleri durduruldu. Gerçek birlikte çalışma denemesinden
önce paralel AOS oturumunun GPU çağrılarını ortak broker'a geçirmek veya
koordineli bir test aralığı ayırmak hâlâ gerekiyor. Kullanıcıya iletilen
koordinasyon sorusunun yanıtı bekleniyor; CPU geliştirmesi sürüyor.

Luna kolları holdout, eğitim ve AOS deneme sürücüsü üzerinde ayrı
kopyalarda çalışıyor. İzole AOS yamaları henüz canlı AOS'a uygulanmadı.
Bu hazırlık gerçek araştırma, eğitim veya birlikte çalışma kabulü değildir.


## 2026-09-27 — V4 sürücüsünün CPU düzeltmeleri

İzole V4 deneme sürücüsünde başlatıcı/readiness zaman aşımı sonrası exact
generation yakalama, kendi süreçlerini temizleme ve drain hatasının başarıyı
geçersiz kılması düzeltildi. İki sistemin ilerlemesi artık aynı aktif aralıktaki
provider ilerlemesi ve broker ticket geçişleriyle denetlenir. Dondurulmuş kaynakta
11 CPU sözleşme testi ve Ruff exit 0; kanıt
`review-evidence/aos-lab-coexistence-v4-cpu-review.json`. Gerçek AOS/GPU/model
çalıştırılmadı ve canlı AOS kaynakları değiştirilmedi. HUMAN typed Lab start,
M0.AOS.5 için gerçek modelin başlatma kararı sayılmaz.

GPU/AOS koordinasyon yanıtı hâlâ bekleniyor. Holdout bütçe/recovery düzeltmeleri
ve Farm B kaynak incelemesi bağımsız CPU kollarında sürüyor;
`32-holdout-integration-review.md` kapsamı ve eksikleri kaydeder.

## 2026-09-30 — izole gerçek CPU restart/stop ve dar AOS yaması

`review-evidence/aos042-wire-stop-recovery.patch`, dondurulmuş birleşik AOS
kaynağına iki ek taşır: Lab status yanıtındaki typed `purpose` alanı ve yeni
açık HUMAN binding için session/generation ile bağlı stop idempotency anahtarı.
Önceki süreçten kalan `uncertain` intent değiştirilmez; aynı güncel binding
altındaki stop tekrarı aynı action'ı kullanır. Yama canlı `/home/cachyos/aos`
kaynağına uygulanmadı. Paralel AOS çalışmasıyla bu dar dosya değişikliği
koordine edilerek ayrıca uygulanmalı ve doğrulanmalıdır.

Gerçek ayrı AOS süreçleri, SQLite reopen, Lab HTTP, lease yarışı/eşzamanlı
start ve TCP console outage/rebind yolları izole CPU koşusunda ölçüldü.
R2'nin 21 başarılı kontrolü sonrasındaki barrier timeout sonucu **exit 1**
olarak korunur; R3 ayrı devam koşusu aynı Lab run üzerinde yeni HUMAN lease,
explicit recover, iki idempotent stop ve foreground için **10/10, exit 0**
verdi. İlk stop-intent recovery hatası da ayrı başarısız kanıttır.

Normal Director stop öncesinde aktif, sonrasında aynı sahipli unit inactive
ve MainPID=0 gözlendi; dispatcher dış bekleyicisinin **exit 1** sonucu başarı
olarak sunulmaz. Gerçek Scorer claimed/inflight aralığı yakalanmadı. Run
ilk stop sonrasında `stop_requested` durumundaydı; ilk gözlem terminal
raporu kanıtlamıyordu. Gerçek Scorer inflight drain, model/GPU/public veri
ve tam M0.AOS.2/.3 kabulü açık kalır. Fixture desktop ve karar motoru
açıkça etiketlidir.
Kaynak ve dar icra özeti:
`review-evidence/aos042-cpu-lifecycle-summary.json`.

R4 teslim kopyası, ölçülmüş R3 kaynaklarını değiştirmeden yalnız ilgili
MANIFEST hash satırını güncelledi. Tam ek yama
`review-evidence/aos042-wire-stop-package-r4.patch` içindedir. Ayrı temiz
test kopyasında check/apply exit 0 ve 1095 kaynak üyede byte eşliği;
required package validation **4360 kontrol, session 36491 / exit 0**.
`review-evidence/aos042-package-r4-summary.json` icra bağını taşır.
[Bağımsız ve AOS opt-in başlatma](63-aos-opt-in-start.md) iki yolu ve özel
yapılandırma şartlarını belgeler; normal GPU/Decider bootstrap bu teslimatta
çalıştırılmadı ve canlı AOS'a yama uygulanmadı.

Aynı run için normal `director recovery inspect` exit 0, ardından
`director recovery apply` session 92579 / terminal `19632a` / exit 0:
`finalized_stopped`, bağımsız finalizer exit 0 ve recovery completed.
Ayrı gerçek API readback `8f1b36` / exit 0, HTTP 200 ile `stopped`
durumunu ve rapor hash
`18f12a5d2d5efa712eb324d06f7bfe38d711152fd351f9724198b5f760ae0825`
eşliğini doğruladı. Önceki dispatcher exit 1 korunur; otomatik tek stop
isteğiyle terminal kapanış ve Scorer inflight drain hâlâ kanıtlanmadı.

## 2026-10-02 — doğrudan oturum koordinasyonu

Scientist oturumu AOS oturumuna doğrudan mesaj gönderip yanıtlarını okuyabiliyor.
Dosya sahipliği korunuyor: Scientist kendi kodunu ve yapılandırma çıktılarını;
AOS kendi kaynaklarını, statik kontrollerini ve izole çalışma alanını hazırlıyor.
Ortak GPU kabul koşusunun tek yürütücüsü Scientist oturumudur. AOS'un kendi
aktif kullanıcı işleri ve süreçleri bu oturum tarafından değiştirilmez.

- Scientist kaynak referansı `d8bc847c7a12140ab96597d886f9608b0e3af630`;
  AOS kaynak referansı `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
  Bunlar inceleme referanslarıdır; sonraki kaynak değişiklikleri yeniden pinlenir.
- Gerçek V5 eski belirsiz istek kapanışı iki oturumca doğrulandı.
  Ayrı immutable closure kullanır; eski intent satırını yeniden yazmaz.
  Bu sonuç GPU release veya yeni inference yetkisi değildir.
- Scientist `0041` boş baseline stop dalını uygular ve bağımsız SQL incelemesi
  yürütür. İlk odaklı CPU doğrulaması 154 test, exit 0; yeni gerçek PostgreSQL
  ve native worker kapanış kabulü henüz çalıştırılmadı.
- AOS güncel native factory/policy karşılığını ve statik model kontrollerini
  paralel hazırlar. Scientist'in yeni ortak paketi `enabled=false` durumundadır;
  eski runtime yetkileri yeniden kullanılmaz.
- Doğru mevcut Decider Python ortamında model dosyaları ve bağımlılık sürümleri
  CPU kontrolü exit 0 verdi. Bu, tüm bağımlılık dosyalarının byte kabulü veya
  Bonsai projection kabulü değildir. Model yüklenmedi ve GPU tahsisi yapılmadı.

Karşılıklı aktarım: kaynak/manifest/yapılandırma hash'leri, sözleşme ve capability
uyumu, mevcut principal ve süre sınırları, ardından tek scheduler rezervasyonu.
Teyit beklerken bağımsız CPU/stop işleri sürer. Yeni inference yetkisi, karşılıklı
pin incelemesi ve aktif kullanıcı işi kontrolü tamamlanmadan açılmaz.

### Güncel görev paylaşımı — 2026-10-02

Scientist, kullanıcının doğrudan haberleşme talebini AOS oturumuna iletti;
AOS'un candidate-v2 inceleme sonucu okundu: **PASS**. Bu paket için aynı
incelemenin tekrar tamamlanması beklenmiyor. Scientist HEAD
`279e33ba2a7a192f1392618d21d904aeb6c2140f`, AOS inceleme referansı
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.

| Taraf | Devam ettiği iş | Karşı taraftan beklediği |
| --- | --- | --- |
| Scientist | Tam sahiplik denetimli foreground yürütme/onay hazırlığı; ardından sınırlı araştırma, bağımsız puanlama ve cleanup kanıtı | Yalnız sözleşmeyi veya pinli runtime kaynaklarını etkileyen değişiklik bildirimi |
| AOS | Kendi ürün/adaptör ve CPU işleri; kendi kaynaklarının sahipliği | Scientist'in gerçek koşu kimlikleri, sonuç ve cleanup kayıtları |

Pin dışındaki bağımsız işler diğer oturumun onayını beklemez. Pinli bir
kaynak değişirse yalnız etkilenen ortak kabul paketi yeniden doğrulanır.
GPU koşusunu Scientist tek başına yönetir; AOS bağımsız GPU kabul koşusu
başlatmaz. Diğer oturumun dosyaları veya süreçleri değiştirilmez.

Genel Operator eylemi **10 saniye** son tarih kullanır; hello görevi
yazmada **70**, okuma/doğrulamada **60 saniye** son tarih verir. Konsol
onay süresi ve eylem son tarihi birlikte geçerlidir. Lab onayı **60 saniye**
geçerlidir; bunlar native artifact veya araştırma bütçesi süreleriyle aynı
değildir. Operatör hazırlığı bu süreler başlamadan tamamlanır. Bir onay
süresinin dolması yeni yetki üretmez ve aynı belirsiz isteğin kör tekrarına
izin vermez. Gerçek model/GPU kabulü hâlâ açıktır.

AOS bu paylaşımı aldı ve yeni ortak engel bulmadığını bildirdi. Süresi
dolmuş veya owner/generation bilgisi değişmiş Lab onayları için iki CPU
regresyonu exit 0 verdi. İlk foreground özetindeki 10 saniye genellemesi hello için yanlıştı:
`operator.py:291` yazma/doğrulama eylemini 70/60 saniyeye, satır296 okuma
eylemini 60 saniyeye ayarlar. Gerçek pending envelope bu ayrı süreyi
doğruladı; operatör bu mevcut süreyi kullanır, yenilemez. Bu CPU sonuçları gerçek GPU
kabulünün yerine geçmez.

### 2026-10-02 — doğrudan oturum koordinasyonu ve ilk native ilerleme

AOS oturumuna gerçek koşu kimlikleri, aktif pinleri koruma beklentisi ve
Scientist'in tek GPU kabul yürütücüsü olduğu doğrudan iletildi. AOS kendi
CPU/ürün işlerini bağımsız sürdürebilir; ortak runtime kaynak değişiklikleri
önce haberleştirilir. AOS, ilk başarılı hello görevini ve dosya içeriğini
kendi kayıtlarından salt okunur doğruladı.

- İlk native hello: gerçek model çağrısı, başarılı terminal trace ve tam
  dosya içeriği doğrulandı. Bu tek görev tam birlikte çalışma kabulü değildir.
- Typed Lab araştırması: `6dd84567-09f7-4fd1-b427-e80c9d17bb28`, bir öneri,
  600 saniye ve 30000 token bütçesi. `proposal:1`, tamamlanmış provider
  denemesi ve `primary_measured` checkpointları mevcut; sentetik mod verisi.
- Koşu `execution_exception` / `RuntimeError` sonrasında `stop_requested`
  kaldı. Yaklaşık 360 saniyelik gözlem bütçe aşımı diye sunulmaz.
  Otomatik recovery `first_stop_deadline_unavailable` ile bekliyor;
  terminal rapor ve AOS rapor readback kabulü henüz yok.
- Director inactive/MainPID 0. Koşuya bağlı model child inactive/MainPID 0;
  eski PID ve cgroup kaybolmuş. Canonical Lab tahsisi `done`, fencing token
  22; bağlanan VRAM tepe gözlemi 12760 MiB. Bunlar stop SQL kapanışının
  tamamlandığı anlamına gelmez.
- İkinci AOS okuma görevi preflight reddiyle başlatılmadı. Bu senaryo
  eşzamanlı adil ilerleme veya kontrollü iptal kabulü diye sayılmaz.

Eksik stop event/deadline incelemesi yalnız Scientist tarafında yürür.
Mevcut yetkiler veya deadline yenilenmez; aynı araştırma yeniden başlatılmaz.

### Kalıcı ortak masaüstü için hazırlık sırası — 2026-10-03

AOS oturumuyla tek çağıran servis adı
`swapp-aos-gpu-shared-desktop-default.service` üzerinde uzlaşıldı. Scientist
launcher değişikliği yalnız ayrı inceleme ağacında hazırdır; MAIN desteği
veya çalışan shared servis sayılmaz. Eski 136/141 kaynak teyidi yeni
manager/host dosyalarını kapsamaz.

Sıra: inert plan → açık, ayrı scope provisioning → taze ortak kaynak/config
incelemesi ve sınırlı runtime bağlama → exact shared start. Provisioning
yalnız yeni app-session/workspace dizinlerini oluşturur; DB, token, socket,
servis, capability veya süre yetkisi üretmez. Mevcut AOS kullanıcı oturumu
bu hazırlıktan etkilenmez.

Scope kaydı plan/template hash'ini, app-session ve exact yolları, dizinlerin
device/inode/UID/0700 kimliğini ve mevcut `descriptor-workspace-v1`
kimliğini taşır. Scientist incelemesi actual canonical/nofollow dizinleri
ve başlangıç boşluğunu yeniden doğrular. Fresh activation öncesinde native
admission kapanışı ve fiziksel GPU dışlama ayrıca gerekir. Salt idle veya
quiesce yeterli değildir.

Yeni nested manager kapsamı eski flat 11-dizinli özel v8 binder'ının
yerine geçirilemez; ayrı, tutarlı kaynak/config bağlaması hazırlanmalıdır.
Start hazırlanmış inode'u doğrular; yeniden mkdir/adoption yapmaz. Kalıcı
kimlik mutable dizin mtime/ctime değerine bağlanmaz. Bu kayıt koordinasyon
kararıdır; gerçek shared başlangıç, GPU paylaşımı ve iptal kabulü açıktır.

### Ortak çalışma alanı yolu incelemesi — 2026-10-03

Gerçek AOS checkout incelemesinde workspace ve ana DB yolunun tek başına
izolasyon sağlamadığı görüldü. Üzerinde uzlaşılan kapsam 19 yoldur:
workspace, iki ayrı DB yolu, web-applications, knowledge, web-tasks,
form-plans, form-values, form-states, routes, static, readonly-data,
console-assets, site-knowledge, site-skills, site-page-seeds,
remote-route-reviews, remote-json-reviews ve json-page-seeds.
`console-assets` çalışma sırasında yazılan bir dizindir. Ek altı store için
entrypoint argümanlarının mevcut console parametrelerine aktarılması gerekir.
AOS oturumu bu kaynak değişikliklerini uyguluyor; Scientist AOS dosyalarını
veya süreçlerini değiştirmiyor.

Provisioning yalnız session/workspace ve kimlik makbuzunu oluşturur;
diğer yollar ortak inceleme sonrası çalışma sırasında oluşturulabilir.
Makbuz activation, factory ve retained config pinlerinin üçünde de yer
almalıdır. Scientist başlangıcı da workspace device/inode/UID kimliğini
`module.main` öncesinde bağımsız doğrulamalıdır; eksik dizinin yeniden
oluşturulması kabul değildir. Bu yeni kaynaklar için eski 141-dosyalı ACK
historik kalır; yeni closure karşılıklı eşleştirilecektir.

İnert template'te broker kimliği gözlenmemiş olarak boş bırakılabilir;
bu bir çalışma yetkisi değildir. Aktifleştirme gerçek socket peer kimliği,
güncel broker generation ve taze capability gerektirir; broker yoksa
başlatma reddedilir. Aynı canonical GPU scheduler kullanılır. Önerilen
ayrı proje `scientist-shared-v1` ve loopback portu `18866` henüz başlatılmış
ortak çalışma kanıtı değildir. Varsayılan native AOS yolu GPU admission'ı
atlayabildiği sürece gerçek GPU kabulü bekler.

### Transitive kaynak ve Btrfs hazırlık düzeltmesi — 2026-10-03

AOS'un yeni ortak masaüstü seçimi önce 86, entrypoint/host import zinciri
incelenince 156 dosya olarak eşlendi. Son v3 adayının özel kayıt hash'i
`e955194e47e14416797aa58eaf632159c7d4e503e7f394e09053f493115f84c6`;
156 gerçek kaynak hash'i Scientist tarafından tekrar okundu, fark yok.
Bu kaynak eşleşmesi çalışma yetkisi veya eski 141-dosyalı ACK'nin yenilenmesi değildir.

Gerçek Python 3.14/Btrfs kontrolü, parent FD açıldıktan sonra oluşturulan
workspace'in `listdir(fd)` ile ilk okumada görünmediğini yakaladı. AOS
sahibi retained FD üzerinde her dizin sayımı öncesi seek(0) ile düzeltti;
kimlik/nofollow sınırları korunur. V2→V3 yalnız bu dosyanın hash değişimidir;
AOS oturumu Btrfs ve tmpfs üzerinde 20'şer odaklı kontrol bildirdi. Actual
named session/workspace provisioning bu kayıt anında yapılmış sayılmaz.

Çağırıcı için beş alanlı `shared_scope` sözleşmesi kararlaştırıldı:
plan path/hash, provision path/hash ve activation path. Activation hash'i
launch girdisine geri yazılmaz; sabit session launch-intent kaydından
okunur ve activation'ın çağrılan launch path/hash bağı doğrulanır. İlk
kontrol ve `module.main` öncesi tekrar, aynı workspace device/inode/UID'yi,
boş workspace'i, exact session içeriğini ve mevcut AOS builder'ının ürettiği
19-yollu desktop argümanlarını doğrular. Eski acceptance caller korunur.

Provision hash'i activation ve factory config map'lerinde zorunludur.
Retained review path/hash çifti varsa üçüncü config map'e de bağlanır;
kısmi çift reddedilir. Gerçek ortak kabul profilinde retained çifti ayrıca
zorunludur. Başlatma öncesi sonlu izin, başlatma sonrası gerçek caller
PID/Invocation/GPU admission binding'i yerine geçmez. Üretim native dışlama,
activation/cleanup bileşimi ve gerçek GPU devir/iptal kabulü hâlâ açıktır.

### Scientist gerçek MAIN çağırıcı — 2026-10-03

Astra/high üretim kod incelemesi ardından yalnız Scientist MAIN
`scripts/aos_native_launch.py` dosyasına aynı baytlar taşındı. HEAD
`55c5300600112ab823f76ec434029d6dc23e513c` değişmedi; yerel fark vardır.
Yeni çağırıcı SHA-256:
`c18efb89584946975f3740b071d2c2a3eca106ea44ae07b6a83060a267a03495`.
Eski `436516d2…` çağırıcı pini bu çalışma için kullanılamaz. AOS oturumuna
actual pin verildi; inert template/plan/provision bu pinle hazırlanabilir.

İzole kaynakta 117 odaklı kontrol, gerçek AOS Python 3.14 interpreteri ve
Btrfs üzerindeki plan/provision/claim yardımcılarıyla geçti. GPU/native
çağrıları testte başlatılmadı; authority geçişleri CPU fixture'larıdır.
Kullanılan küçük pytest bağımlılık alanı özel ve cache'ten hazırlanmıştır;
çalışan AOS veya Scientist ortamlarına paket kurulmadı. Standart AOS'suz
Scientist testleri için bu harici entegrasyon kontrolleri ayrı opt-in'dir.
Native AOS policy, authority, service ve GPU başlatılmadı; pinli kaynak
hazırlığı gerçek GPU kabulü değildir.


### V4 gerçek ayrı çalışma alanı ve son çağırıcı kontrolü — 2026-10-03

Public `python -m aos.local_app` hazırlığında named instance'ın canonical
modüldeki default instance ile karıştığı yakalandı. AOS oturumu `__main__`
girişini canonical `main` fonksiyonuna bağladı. V3→V4 yalnız
`src/aos/local_app.py` değişir; SHA-256
`5ec835173c6a0bd773713e13b4282eb80386935f893d80f5d7e8ef007ec0559d`.
V4 seçili 156 kaynak kaydı SHA-256:
`f004bdce68e000b784d91c61d76b1b3711c9b332dcb7f470372001725a309054`.
Scientist tüm hash'leri tekrar okudu; fark yok.

AOS oturumu `scientist-shared-v1` / loopback `18866` için gerçek public CLI
ile yalnız hazırlık yaptı. Scientist `load_plan`, `verify_template_files`
ve `load_provision` ile bağımsız doğruladı: 227 kaynak / 3 config pini,
0700 boş workspace, doğru device/inode/UID ve yalnız receipt+workspace
içeren session. Servis, runtime, token, activation ve GPU işi başlatılmadı.
Özel template / plan / provision hash'leri sırasıyla:

- `8318bee3d666c0c62f77bd74236d669b186fd7720d0db3ba0a7bc9f6d8fc65cc`
- `fbced4bb609b2efdeade898231c8287db9238ab91836f5eea2c8159eca536c45`
- `07270c77f5b1c9cf7d6207638273746713f8dcfe134b07372cde9d78a3054f24`

Scientist bağımsız hazırlık okuması kanıt hash'i:
`1aa7da6c2cbab6dbdbf076d0b55a59a5f7ba119544a409bd50cfed6726f21f9f`.
Astra/high onaylı iki çağırıcı testi MAIN'e tam baytlarla taşındı.
Standart Scientist 3.12/AOS'suz koşu: **17 geçti / 42 açık opt-in skip**;
son V4/AOS 3.14 gerçek yardımcı koşusu: **59 geçti / sıfır skip**, exit 0.
Özel kanıt hash'leri sırasıyla `4065695e510a93ac99af6ad43ee70b0f64fda4897d6e1bc4c815415c093b65b7`
ve `4f2005d9ce078ffb6108e8d9793b1a28777eb09b71f5a034db448db1e9d5f5d2`.
Önceki V3/117 sonucu değiştirilmedi. Bu odaklı kontroller MAIN için yeni
tam kalite kapısı sayılmaz. FEATURE 0.44.0 tam kapısı ayrı teslim kanıtıdır.

AOS'a beklenti: default native yolu ve Lab service dahil tüm admission
kapılarını kapsayan, özgün generation'a bağlı drain/dışlama; sonlu taze
shared launch yetkisi; başlatma sonrası gerçek caller/broker binding'i;
ayrı önce/sonra cleanup kanıtı. Mevcut scheduler tek tahsis otoritesidir.
Idle/quiesce GPU bırakma kanıtı değildir. Hazırlık uyumu yeni runtime yetkisi
veya gerçek birlikte çalışma kabulü olarak sunulmaz.


### Ortak admission drain sonrası Scientist adaptörü — planlanan dilim

AOS oturumu Console/scheduler/Lab yeni iş kabulünü aynı generation'a bağlı
latch ile kapatan değişikliği açtı. V4/227 kaynak hazırlığı tarihsel kalır;
mevcut workspace yeni kaynak için yeniden benimsenmez. AOS süreçlerine
Scientist müdahale etmez.

Astra/high kararı: `scripts/aos_shared_desktop_host.py` mevcut
`SharedDesktopHost` activation/cleanup callback'lerini ve mevcut review
sözleşmelerini bağlayacak; tek Sol 6.1/high sahibi ve odaklı test dosyası.
Bu dosya henüz uygulanmış değildir. Uygulamadan önce AOS'un somut drain
DTO/API'si gerekir: exact `SharedServiceBinding` ve runtime/session'a bağlı,
yeniden okunabilir canonical receipt/SHA; üç admission kapısının kapalı
kalması ve pending/reserved/active/unresolved sayımları. Makbuz after_stop'ta
da okunabilir olmalıdır. Bool idle veya kullanıcı JSON'u yeterli kanıt değildir.

API context'in native inference/GPU release izinleri false kalır; shared
servis start/stop hakkı API erişiminden türetilmez. Sonlu ayrı yetki ve
native dışlama bağımsız doğrulanır. Gelecek caller PID'si başlatma öncesi
uydurulmaz; mevcut launcher gerçek caller doğrulamasını korur. Before/after
cleanup aynı generation/state'e bağlanır, özgün timeout yenilenmez.


### Scientist özgün API yetki süresi — uygulandı, 2026-10-03

`JointLabCapability` API/token/kaynak canlılığını doğrularken pinli context'in
özgün boot ve bitiş zamanını her çağrıda kontrol etmiyordu. İzole regresyon
bu eksikliği önce `DID NOT RAISE` ile yakaladı. Mevcut
`scientist.joint-api-authorization-context.v1` için public verifier ve
callback bağlantısı uygulandı; Astra/high incelemesinden geçen dört dosya
Scientist MAIN'e tam baytlarla taşındı. Yeni yetki türü oluşturulmadı.

- `scripts/aos_joint_api_authority.py`:
  `a8d9185ffc7b06d0458197f1450897bbe3e0ef131a7badb6f6a5987b9a5f184a`
- `scripts/aos_joint_lab_hooks.py`:
  `3ca32027a48c71e7670d58fc9f3849f250104d10eca43e952be2e2f8010aaa74`

Yeni helper kaynak pinlerine eklenmelidir; önceki 67 Scientist dosyası
bu seçimde 68 olur. Launcher `c18efb89…` değişmedi. Context hash'ine bağlı
tek dosya, özgün boot ve en fazla 3600 saniyelik finite süre, owner/origin/
URL/suite/program/unit/token ve bütçe eşleşmesi zorunludur. Constructor
client hazırlamadan önce, her canlılık kontrolünün iki yanında ve callback'in
GET çağrıları öncesinde doğrulanır. HTTP deadline kalan özgün süreyle kısılır;
IO sırasında expiry/revoke son kontrolde reddedilir. Eski süresiz belgeye
zaman alanı uydurulmaz, retry yetki süresini yenilemez.

94 odaklı kontrol (37 yeni + 57 mevcut), Ruff/Bandit/compile/diff exit 0.
API kimliği ve AOS istemcisi testlerde inert fixture'dır; gerçek kaynak ve
context dosyalarının doğrulaması çalışır. [Seçilmiş kanıt](review-evidence/joint-api-original-context-fix-20261003.json).
Bu sonuç yeni tam MAIN kalite kapısı veya gerçek runtime/GPU kabulü değildir.

Kapsam callback'in oracle GET yoludur. Ayrı AOS POST istemcisinin bütün
çağrıları veya gerçek API sunucusunun süresi bu teslimle denetlenmiş sayılmaz.
Süre dolması start/status/stop/report iznini yenilemez; cleanup/GPU hakkı
üretmez. Mevcut bütçeli Director durdurması ve ayrı original-target cleanup
sınırları korunur. Servis, FEATURE ürün kodu ve GPU çalışma durumu değiştirilmedi.

AOS tarafındaki final-seal adımı henüz adaydır: normal drain sonrası izinli
status/report/stop çağrıları yeni local control başlatabildiği için anlık
`local_controls_drained=true` destructive kapanış kanıtı değildir. Tüm yeni
control kabulünü kapatan seal ve bağımsız native GPU dışlama kanıtı gerekir.


### AOS V5 final-seal sözleşmesi — salt okunur eşleşti

AOS oturumu drain/seal kaynaklarını dondurdu. Seçilmiş 161 dosyalı V5
paketinin hash'i `9ea15d364611ce26e1e55afe9f88ebba11c6cd38364f332a5a86f7843ccd15a4`;
Scientist tüm dosya hash'lerini tekrar okudu, fark yok. V4'ten dört kaynak
değişti, `shared_drain.py` ve dört şema eklendi; kaynak silinmedi.

`POST /api/shared/drain` ve `/api/shared/drain/seal` aynı altı alanı alır:
request_id, session_id, runtime_id, owner=AGENT, lease_id, generation.
`SavedSharedDrainObservation` event id/SHA ve canonical process-bound
receipt taşır. Before-stop yeni alınan sealed receipt'in
`admission_closed`, `local_controls_drained`, `cleanup_controls_closed`
alanlarının üçü true, blocker listesi boş olmalıdır. Gerçek process,
SharedServiceBinding unit/Invocation/cgroup ve runtime/controller kimliği
ayrıca doğrulanır. Normal drain cleanup kontrollerini açık bırakır; seal
bunları da process için geri açılamaz biçimde kapatır.

Tarihsel GET ve `read_shared_drain_receipt` helper'ı exact session/event/SHA
ile salt okunur scoped SQLite okuması sağlar. After-stop aynı kayıt tekrar
okunabilir; bu GPU veya remote-job cleanup kanıtı değildir. Makbuzda GPU,
native exclusion ve remote-job-stopped alanları false kalır. Değişmemiş
default native UI'nin broker dışında gelecekte yeni iş kabul etmesini
engelleyen kanıt hâlâ yoktur; mevcut kullanıcı servisine müdahale edilmedi.
Yeni birleşik source/config paketi ve sonlu yetki gerekir; önceki V4/227
hazırlığı bu yeni kaynaklara yetki sağlamaz.


### Gerçek native dışlama için kalan producer

AOS oturumu mevcut default native process çalışmayı sürdürürken kalıcı
dışlamanın kanıtlanamayacağını teyit etti. Geçiş hazırlığı exact hedefler,
aktif iş varsa ret, admission kapanışı, owned süreç/worker cleanup ve geri
dönüş/UI etkisini kapsayacak; canlı geçiş henüz onaylı veya yürütülmüş değildir.

Astra/high incelemesine göre production host adaptörü için eksik parça,
exact handover/shared-plan pinlerini alıp güncel native admission fence'ini,
aynı boot ve özgün süreç/servis kimliklerini, source/config/maintenance
bağlarını ve bağımsız fiziksel cleanup gözlemlerini döndüren salt okunur
kanıt producer'ıdır. Yeni native start/restart yolları da bu fence'e uymalı;
fence cleanup ve ayrıca yetkili release bitmeden otomatik açılmamalıdır.
Expiry start/model hakkını bitirir; ayrıca yetkili bounded cleanup eski
çalışma iznini yenilemez. İsim/sözleşme karşı tarafta henüz son hâline gelmedi.
SessionInspection, final seal veya eski PID'nin yokluğu bu kanıtın yerine
geçmez. Yalnız boolean döndüren geçici adaptör tamamlanmış iş olarak yazılmadı.


### AOS native devir ön incelemesi — salt okunur doğrulandı

AOS v2 preview (`27f1b790…`) ve aktarım kaydı (`85b3fdc9…`)
Scientist tarafından bağımsız dosya hash okumasıyla eşleşti. Anlık yerel
idle gözlemi var; execution, native exclusion, GPU release ve Scientist
reservation alanları false. Ortak SSH cgroup sahip olunan shutdown hedefi
değildir. Kullanıcı geçiş onayı, sürdürülebilir native admission engeli,
fiziksel cleanup ve canonical rezervasyon açık kalır. Canlı servis değişmedi.

AOS sonraki kaynak diliminde native worker ömrü boyunca tutulan kilit ve
yeni native start/restart yollarını kapsayan inhibit hazırlıyor; broker
worker mevcut TurnGate yolunda kalır. Bu ikinci GPU allocator değildir.
Yeni source map ve gerçek exclusion producer olmadan host adaptörü veya
GPU kabulü hazır sayılmaz. [Hash ve kapsam kaydı](review-evidence/joint-api-original-context-fix-20261003.json).

## 2026-10-03 — Public DEV CPU teslimi ve sonraki native adım

Scientist 0.45.0 gerçek SKAB DEV UI koşusu `92ae603c-e367-422b-92fa-4fd567f80548`
283,41 saniyede tamamlandı; 9 baseline + 3 OPTICS skoru, LSH guard reddi.
OPTICS KEEP, en iyi baseline ham skorunun geçildiği anlamına gelmez. Director,
12 Scorer işi/cgroup ve sandbox temiz; kuyruk boş. AOS supervisor/backend
42512/42513 özgün kimlikleri korundu. Model/GPU çağrısı sıfır.

AOS oturumu staging alanında broker dışı tüm repo-managed native girişleri
(legacy başlatma, worker ve probe dahil) reddeden ortak profil yaması
hazırladığını bildirdi; broker S1/S2 korunacak. Bu bildirim uygulanmış host
dışlama kanıtı değildir. Tam envanter, executable maintenance, sahipli fiziksel
cleanup ve sonlu özgün yetki/config pinleri Scientist çağırıcısına bağlanmalı.
Canlı AOS değişmedi; kontrollü eski oturum kapanışı için somut paket ve
kullanıcı onayı gerekir. İkinci GPU otoritesi oluşturulmaz. Ortak gerçek
GPU kabulünün tek yürütücüsü Scientist; GPU HOLD devam ediyor.

[CPU teslim kanıtı](120-public-dev-cpu-study.md). Bu CPU sonucu native
GPU başlatma izni veya birlikte çalışma kabulü değildir.

### Native exclusion tüketicisi: mevcut durum ve öneri

2026-10-03: `read_native_exclusion` host tüketicisi henüz uygulanmadı;
onaylanmış wire şeması veya hazır endpoint yok. AOS oturumuna bu durum açıkça
iletildi. Önerilen salt okunur imza:
`read_native_exclusion(expected_handover_sha256, expected_shared_plan_sha256, *, deadline)`
→ canonical kanıt + SHA. Zorunlu öneri alanları: schema/version, aynı boot,
özgün issued/expires ve çağrı deadline sınırı; handover/plan/source/config/
maintenance SHA; exact unit/InvocationID ve PID/start_ticks; envanterli
native girişlerde halen etkili yeni start/restart engeli; bağımsız sahipli
worker fiziksel yokluk kanıtı. Eksik, süresi geçmiş veya değişmiş kanıt
kabul edilmez. Tarihsel receipt/idle yeterli değildir. Süre bitimi native
çalışmayı otomatik açmaz. Bu arayüz acquire/release/yetki üretmez; canonical
scheduler tek tahsis otoritesi kalır. AOS tam profilinden somut producer
şeması önerildikten sonra Scientist tüketicisi aynı sürüme bağlanacak.

### Ortak şema için süreç rolleri ayrımı

AOS ile teyit edildi: emekliye ayrılacak eski oturum dedicated service
değildir; paylaşılan SSH `session-3.scope` içindedir. Bu cgroup sinyal/stop
hedefi olmaz ve ona InvocationID uydurulmaz. Eski oturum kapanış kanıtı
özgün manager session, supervisor/backend ProcessIdentity (PID/start_ticks/
boot ve sahiplik) ve bağımsız sahipli worker yokluğuna bağlanır. Yeni ortak
çağırıcı ise gerçek dedicated unit/InvocationID, süreç kimliği ve pinli
plan/source/config ile doğrulanır. Producer önerisi bu rolleri ayrı tutacak.
Bu teyit canlı kapanış veya GPU yürütme izni değildir; kanıt tahsis yetkisi
üretmez, özgün sonlu sürenin bitişi native çalışmayı yeniden açmaz.

### Shared-only aday kaynak incelemesi

Astra/high salt okunur incelemesinde manifest `d52a57e8…`, patch `a0cbf60e…`
ve handoff `0704012f…` eşleşti; bellekte patch rekonstrüksiyonuyla 41 önce/sonra
ve 13 korunmuş dosya pini, 86 politika doğrulandı. Somut bypass veya broker
uyumsuzluğu bulunmadı. Yeni promoted kaynak/factory closure tüm 41 ret dosyasını
ve brokerın import ettiği `services/decider/worker.py` dosyasını kapsamalı;
eski 161 kaynak pini yeterli değildir. Bu koşul AOS oturumuna aktarıldı.
Aday canlı sisteme uygulanmadı. Exact maintenance, gerçek producer DTO/uygulaması
ve aynı sürüme bağlı Scientist consumer henüz açık; GPU HOLD sürüyor.
