# AOS ile doğrudan koordinasyon — 2026-10-01

Kullanıcının talebi üzerine iki gerçek Codex oturumu doğrudan mesajlaşıyor.
AOS kaynak/çalışma alanı sahibi AOS oturumu; Scientist düzeltmeleri ve entegre
GPU kabulünün tek yürütücüsü Scientist. Karşı tarafın aktif kullanıcı servisleri,
prosesleri ve dosyaları değiştirilmedi. Kaynak hazırlık ile admission ayrıdır.

## Karşılıklı doğrulanmış hazırlık

Scientist HEAD `1fbd76f47dc4f30039fa40998ae445df84995f0f`;
AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`, dirty reviewed snapshot.
AOS son kaynak pinleri:

- selected source: cc62d4955b125ea30f46936d416d42b09798f103f2d5f9560c40f7e52794e90f
- tracked diff: 99c2a3dd4bde35c4881b17fc9e0225dd81480da07e8dd4f01d98cafa89bb708b
- selected untracked: de413fed26b18d054847e9da5363404893564dd5e0918a010a0de518dd10f362
- runtime manifest: 4a2149b0682607fa92d7cac6aa04faad94df0ce5788a7fa5f771a70b47adf7c4

Scientist actual checkout preflight exit3/source_ready=true/admission=false;
receipt SHA9631af3eeda583db90cf04f2e2cd78f867c46681069ac2ba458cffaf5865801b.
AOS ortak review süresince bu kaynakları dondurdu; sonraki drift yeniden review
ister. Actual Decider/Bonsai broker worker dosyaları mevcut; eski
shared-gpu-turns/lab-external CLI bayrakları bu checkout'ta yok. Native factory
hook'ları kullanılır; izole eski yamalı kopya public ana dal yerine sunulmaz.

Source verifier `__call__(selected_bindings, *, deadline=None)` sözleşmesinde
absolute time.monotonic kullanır. AOS kendi5s sınırını verilen kalan bütçeyle
min alır; Scientist kaynak/auth/runtime callbacks aynı inherited bütçeyi taşır,
her callback süreyi yenilemez. Uyumsuz native API Desktop'tan önce reddedilir.
66 focused factory/deadline CPU testi geçti. Bu cooperative fail-closed kontrol,
OS seviyesinde kesin gerçek zaman garantisi değildir.

## Paylaşılan kapalı taslak

Scientist özel dizin:
`data/runtime/aos-joint-direct-review-20261001/disabled-native`.

- policy SHA e5d830ee36f40baba1276223a97be84e9bd5eb6ba9dc0c3139734761397bef46
- profile config SHA81077a0b395a6207df22d16ff489b051df9b22bea1ef90a35246f745670b15cb
- Scientist native source fingerprint6a8a83f54b23886539c896aa0face783a286b0a0a349e0e73c16cc718a6590af

AOS oturumu bu dosyaları salt okunur karşılaştırdı; policy/profile raw SHA,
source map, canonical caller ve üç profile manifest pin'i eşleşti.
Canonical unit `swapp-aos-gpu-joint-acceptance.service`, Slice swapp-gpu.slice.
Policy enabled=false; servis/scheduler oluşturulmadı. Bu hash kapalı taslağa
aittir; enabled policy farklı hash ve açık review gerektirir.

## Devam eden bağımsız işler ve sınırlar

Scientist gerçek inference-stop terminal hatasını regresyon-first düzeltir;
AOS launcher/current-generation/artifact/Lab capability taslağını paralel
inceleyebilir. Kaynak hazırlığı diğer oturumun terminal hatasıyla kilitlenmez.
Yeni GPU kabulü terminal güvenliği doğrulanana kadar başlamaz.

Launcher aynı deadline'ı native broker/caller auth'a aktarmalı; native verifier
çıktısı ve child yaşam döngüsü sınırlanmalıdır. Son launch input/static artifact
requirements/joint Lab review pin çifti karşılıklı incelenmeden servis başlatılmaz.
Mevcut inference wire1/native bootstrapv1/control contractv2/retained evidencev3/
scientist.lab-capability.v1 kullanılır; ikinci GPU tahsis otoritesi yoktur.

Kimlik, invocation, boot/start ticks, generation, execution/profile/policy/source
pin'leri birlikte doğrulanır. Acquire/cancellation/drain/release mevcut canonical
SQLite authority ve physical cleanup/quarantine üzerinden yürür. AOS idle/quiesce
veya stop ACK release kanıtı değildir. Timeout/revoke/cleanup başarısızlığında
allocation fiziksel kanıt olmadan bırakılmaz.

## Kabul durumu

Geçen: kaynak pinleri, CPU ortak deadline, kapalı config karşılaştırması;
Scientist tek LSH yerel-model deneyi terminal raporu; inference stop ACK ve exact
fiziksel cleanup. Başarısız: 7038f5fa terminal SQL/rapor kapanışı.
Çalıştırılmayan: AOS kontrollü görev → GPU devir → Scientist öneri/bağımsız Scorer
→ AOS rapor doğrulama → adil birlikte ilerleme. Model eğitim/adaptör yok.
[Ölçüm ve açık hata](106-mode-agent-and-inflight-stop.md).


## Karşılıklı review sonucu ve yenilenen taslak

AOS oturumu static107kaynak/profile/interpreter review'unu bağımsız yürütür.
İlk review iki gerçek launcher sorunu yakaladı: reaped leader sonrası killpg ve
constructor'ın inherited deadline'ı kullanmaması. İlk regresyon RED; amended
launcher20CPU/Ruff PASS: original leader WNOWAIT ile tutulur, group/session/
startticks+OSchild sahipliği doğrulanır, ownedgroup sinyali verilir, orijinal
session üyeleri bounded0,2s içinde terminal değilse Desktop başlamaz; sonra
leader reaped edilir. Constructor min3s/inheriteddeadline kullanır.

Stable launcher SHA2e12c967ad125678336b81afc17f5085d1e03af91d7bc4b2b5d1f6285430d5ed;
joint hook SHAe540d2f67b3c1ca06ee7b46cf42cb58ca1e12c0d1b5ae5d196bc5e490744f429.
Bunlar deployment/GPU kabulü değildir; AOS amended karşı review bekliyor.

Yeni kapalı taslak disabled-native-amended policy
643624ae0410795a9e799e7bbb9b29e829c194e2836927d6cabf9bbabf0414e8;
Scientificnative source7543351bd3a1b8b421e5e086ccd55ebe732fb4489206bf5f2c1347c086faa8ca.
Policy kaynak listesine launcher/hooks/APIoracle/app/registry eklendi.
Profil pin'i81077… değişmedi. Önceki e5d kapalı taslak superseded.
static-artifact-input.final-review-draft.json SHA
bda78a441cd81ba46a78dedbd0c7113d17eedab50f83aecf6215f3fda0e8982e,107sourceinputs;
original native verification input098550749d14421659972df997203bf10db2316a5278b16fe5f5a6f0c5295a20
değişmedi. Bu sadece pinli review girdisidir; fresh native receipt değildir.

Yeni actual stop retry7af2 baseline engelini aştı, sonraki Scorer finalizer failed.
Exact token16/requestdone/physicalcleanup geçti; terminal SQL/rapor geçmedi.
Scorer gerçek rol/SQL/report sözleşmesi izole Scientist-owned CPU test veritabanında
yeniden üretilerek inceleniyor. Canlı deney kayıtları/deadline değiştirilmez;
AOS statik hazırlığı bu hatadan bağımsız ilerler. Yeni GPU kabulü başlatılmaz.


## Güncel karşılıklı anlaşma ve sıradaki somut iş

AOS amended launcher ve107kaynak static girdisini bağımsız inceleyip kapalı
hazırlık kapsamında kabul etti. Cleanup garantisi pinli, spawn yapmayan native
verifier'ın **orijinal session üyeleri** içindir; arbitrary setsid descendant
veya GPU release garantisi değildir. Interpreter startup/.pth ve tüm transitive
bağımlılık bytes attested diye sunulmaz.

AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`; selected source
`cc62d4955b125ea30f46936d416d42b09798f103f2d5f9560c40f7e52794e90f`.
Son sadece belge/sayaç güncellemesi tracked diff'i
`2f5ebf6823d0cc4fac4fdd8bf22f951363b425c3ce17f502b275f22b5bcfed2c`,
runtime manifest'i
`2fc3fdae44f0b3aa154d3c4e23175f8e6191ce70e7ef3d2374e594bde6364103`
yaptı; selected runtime/unit bytes değişmedi. Önceki99c2/4a214 pin'leri tarihsel.

Karşılıklı incelenen **çalıştırılamayan** hazırlık zarfları:

- native-launch-disabled-preparation SHA
  `5505db5272e67f45681d98723d21d0e4aa034ba99b74562698a1c66dea87ba69`.
- joint-lab-disabled-preparation SHA
  `5f934b64f836f5f7dbe5c338b7f6ee85c37a6fcd28b99061e993a38bcc775b1a`.

Her ikisi enabled=false/runnable=false; API principal henüz provisioned değil.
Scientist actual isolated AOS-principal API generation/token/context hazırlığını
üstlenir. AOS infer broker UDS ve Desktop argv/manifest alanlarını kendi checkout'undan
salt okunur doğrular. İşler bağımsız ilerler; etkin AOS oturumuna müdahale edilmez.
Current local console principal AOS yetkisi yerine kullanılamaz.

Son0eb gerçek stop terminal+physical cleanup geçti; tam iptal bütçe muhasebesi
açık. Önceki7038/7af koşuları başarısız olarak korunur. Bu sonuç hazırlığı
ilerletir; native broker rights/enabled policy/current-generation pin'leri ve
karşılıklı runtime review tamamlanmadan ortak GPU koşusu başlatılmaz.

**Geçen:** gerçek tek LSH model önerisi/bağımsız ölçüm; gerçek repeated inference
stop terminal raporu ve fiziksel cleanup; CPU/source/config review ve kalite kapısı.
**Kalan:** iptal bütçe audit'i, actual AOS-principal API ve runnable native rights/config.
**Çalıştırılmayan:** AOS kontrollü görev → doğrulanmış devir → Scientist öneri/
bağımsız skor → AOS rapor doğrulama → adil birlikte ilerleme. Eğitim/public
endüstriyel kabul ve lisans/genel CI işleri ayrı açık maddelerdir.


### Gerçek izole API hazırlığı tamamlandı

Yeni süre sınırlı CPU-only API: loopback18595,
`lab-aos-joint-api-preparation-20261001.service`,900s TTL/512MiB/CPU100%/Pids64.
AOS principal `aos-joint-20261001` ve bağımsız pinli tek mode-suite registry;
canonical DB korunur. Authenticated GET capability bağımsız DTO ile eşleşti;
original AOS ScientistLabStartup/client GET de geçti. POST/deney/GPU/AOS
aktivasyonu yapılmadı; mevcut kullanıcı servisleri değiştirilmedi.

PID550623/startticks1729361, boot9a4ceebb-0966-429c-ad1f-03eda96dc840,
invocationda74a7ec829743baac093e292e71f299. Bu kimlik **geçicidir**;
TTL/live generation tekrar doğrulanmadan sonraki startup'ta kullanılamaz.
Private kapalı joint review SHA
`74d31c806a99c1bd52eff8fef2f8c09b1e6b58f1c8b369704aedab3d4ca6a348`,
provisioning evidence SHA
`4c12eb225efa181b89ff302a4bc247dfb1f029b3c1581fd230925a70e1b104b3`.
Contextda25457… sınırlı API scope hash'idir, native broker grant değildir.
Native review enabled=false kalır. Principal tek başına1/1800/30000 sınırını
uygulamaz; pinli JointLabCapability/task denetimi dispatch öncesi uygular.
Token değeri/DSN/private evidence yayımlanmaz.

AOS kaynak doğrulaması: inference socket `/run/user/1000/swapp-gpu/broker.sock`,
control socket ayrı olmalıdır. Desktop scientist engine, broker socket ve
pinli decider manifest gerekir; bonsai vision seçilirse pinli bonsai manifest
gerekir. Bunlar closed native composition review girdileridir, etkin unit
kurulumu veya joint GPU kabulü değildir. Sonraki darboğaz native current
broker rights/enabled canonical policy ve runnable composition review'dur.


AOS kapalı native unit önerisi SHA
`76315b273b46cb61a65e4432daca6a158950f7ada2be17be9719a17c2da4a6d6`
Scientist tarafından salt okunur karşılaştırıldı: original manifest hash'leri,
launcher argv aktarımı, iki PYTHONPATH root'u ve ayrı infer/control socket uygun.
Mandatory reviewed env henüz yok; unit kurulmadı/başlatılmadı. Etkinleştirmeden
önce host CPU/RAM/process/runtime sınırlarının unit veya parent slice'ta
somutlaştırılması AOS oturumuna iletildi. Bu karşılıklı review scope'u API/native
yetki grant'i değildir.


### Uzun kabul için yeni API ve somut native aday

18595/900s API yerine mevcut servise dokunmadan ayrı18596/2400s API
provision edildi: unitlab-aos-joint-api-acceptance-preparation-20261001,
PID553716/startticks1765056/invocation4dc8acc7870d4eab89216b45544d44cf.
Kapalı review854cabb…; contextd874d8… sadece API scope'udur.
Native policy65fd345…/jointreview7d8a01…/typedlaunch0d8fcaf… adayları
Scientist-owned private dizinde hazırlanıp AOS'a gönderildi; gerçek broker/caller
generation henüz yok ve servis/GPU başlatılmadı.

Source review iki gerçek başlangıç sorununu buldu: ortak noVNC asset dizinine
yazma ve Bonsai vision için eksik --browser-tasks. AOS oturumu dar fix'i kendi
kaynaklarında yapıyor. Bu source drift'tir; eski candidate/source pinleri
yeni runtime için kullanılmaz, düzeltme sonrası tekrar eşlenir.

Native receipt300s yalnız native model admission yolundadır. Actual AOS
ScientistLabService ayrı JointLabCapability/HTTP API yolu ile uzun iş status/
report doğrulamasını yürütür; native artifact callback çağırmaz. İlk native
görev300s içinde olmalı; sonraki native çağrı expired receipt ile reddedilir.
Özel retained FD verilmediğinden retained-provider-v3 kabulü çalıştırılmadı.
Bu sınırlar native/AOS GPU kabulü yerine sunulmaz.


### Native doğrulayıcı ortam düzeltmesi ve startup'a hazır kaynak

Actual pinned Decider interpreter installed dependency map'i manifest ile aynı:
197 sürüm; kaynak PYTHONPATH AOS/src eklenince yalnız aos-local0.1.0 fazlalığı
çıkıyor. Bounded child'ın inherit ettiği bu kaynak yolu original native
verify_environment reddine yol açtı. RED child distribution regresyonundan
sonra yalnız verifier child PYTHONPATH kaldırıldı; parent ortamı ve tam
native dependency kontrolü korunur. Model/manifest/mevcut venv değiştirilmedi.

21 child testi ve mandatory gate2272 passed/7 skipped/121 deselected,7exit0.
Actual bounded native CPU doğrulama7,755s: model artifact bytes ve dependency
sürümleri geçti; model instantiate/GPU yok, tüm dependency bytes attested değil.
Kanıt SHA778ef9ece430e85d8c3563254dd3f20d0b58fc68bd769916118145811f0c433d.
Bonsai manifest projection pin'i yok; projection_verified=false korunur, bu
sonuç native Bonsai response/GPU kabulü değildir.

Source AOS7ad467…/trackedbb944… sabit. Son reviewed policyb3c257…/
jointce3b58…/static82caa1…/launch814fad…/argv254f88… AOS karşı review'dan
geçti. Actual fresh API18597 PID569234/inv0763aa8…/2400s; her startup'ta
current generation ve kalan TTL tekrar doğrulanır. Native requester/broker
generation ancak gerçek startup'ta yakalanır.

İlk Scientist-owned canonical broker CPU-only açıldı ve hiçbir GPU request
olmadan source repin için exact PID570327/invd62d… doğrulanarak kapatıldı;
PID ve iki socket yokluğu/canonical idle kaydedildi. AOS unit henüz açılmadı;
önceki18595/900s veya18596 API generation'ları yeni kabul için kullanılmaz.


### İlk gerçek native startup ve interpreter boyutu düzeltmesi

AOS transient generationbfbbbb7edefb417c83c00082df859165, MainPID0 ile
Desktop/model/GPU başlamadan başarısız oldu: snapshot default8MiB small-input
sınırı actual UV Python binary30.929.576byte dosyasını reddetti. Interpreter
hedefi için ayrı32MiB sınırı eklendi; source/config8MiB sınırı değişmedi, exact
identity/SHA korunur. Regresyon RED→GREEN, gerçek binary SHA
9544d2a29138833e6177d45dbc57468d37710b5080c901fbb579d53f251cdd6f
doğrulandı. Actual static snapshot0,273s geçti; bu live receipt/admission değildir.
Son mandatory gate2274 passed/7 skipped/121 deselected,7exit0.

AOS karşı review: policyd8bdc745…/joint6ecadd06…/static482b10f1…/
launch83057880…/argv77b01685…, source7ad467… değişmedi. Final actual API
18598/PID585126/startticks2020117/inv3c66fd7094884b3f9a9997c44990f51f
3600s TTL; token/context yenilendi, model/task sınırı1/900/30000 seçildi.
Önceki18597 generation'ı yeniden kullanılmadı.

İlk brokerfixedpolicy0644 hatası strict private-policy guard tarafından
reddedildi; yalnız Scientist-owned private JSON mode0600 düzeltildi, raw
hash'ler değişmedi. Hata/journal saklandı. Broker584527/6d1 ve ilk570327/d62
yalnız own generation doğrulanarak kapatıldı; GPU allocation yoktu. Bu
provisioning/native startup başarısızlıkları gerçek joint GPU kabulü değildir.

### Doğrudan koordinasyon ve maskelenen native açılış hatası

AOS oturumuyla doğrudan mesajlaşma açık; entegre GPU koşusunun tek yürütücüsü
Scientist'tir. AOS kendi kaynaklarını yönetir, Scientist AOS checkout'unu salt
okunur inceler. Runtime seçili kaynak7ad467… sabit kaldı; yalnız AOS
README/STATUS/MANIFEST metrik güncellemesi tracked farkı9f52d7bd… yaptı.
Önceki bb944c… gözlemi tarihsel olarak korunur; belge farkı runtime yetkisi değildir.

İkinci AOS generation7475916b2bff4030b8185a001af6309d terminal exit2:
policy dosyasının bağımsız config listesinde tekrarlanması reddedildi. Yalnız
özel launch girdisinden tekrar çıkarıldı; ayrı policy pin'i ve guard korundu.
Yeni receipt ile üçüncü generationaa0e0a0d19bd4120adf4a8d997891dcd,
MainPID0/exit2,7,446s wall: gerçek serve_desktop.main confirm_runtime kontrolü
maskelenen bir kaynak/capability hatasıyla durdu. Desktop veya model/GPU
başlamış sayılmaz; iki başarısız generation'ın receipt'leri yeniden kullanılmaz.

Paralel salt okunur inceleme profil/deployment/closure/schema/config ve AOS
kaynak pinlerinde uyuşmazlık bulmadı. Kesin neden için Scientist launcher
confirm_runtime denial zincirinin yalnız tür ve function/line konumlarını
yazar; exception mesajı, path, source text veya locals yazılmaz. Orijinal
exception yeniden yükseltilir, callback dönüşü korunur. Hedefli24 CPU testi
geçti; bu tanı GPU kabulü değildir.

İlk tanı kalite koşusundaki tek artifact testi, varsayılan /tmp16GiB tmpfs ile
mevcut20GiB reserve şartı nedeniyle reddedildi (2276pass/1fail). Host proje
diski63GiB boştu; şart gevşetilmeden ayrı Scientist-owned runtime TMPDIR ile
zorunlu kapı yeniden çalıştırıldı. Başarısız koşunun log'u korunur.

İkinci tanı kapısı2270 passed/7 failed/7 skipped/121 deselected; yedi
başarısızlığın gerçek nedeni inceleniyor. Tam kapı geçmedi.
AOS karşı review, özel hazırlık policy haritasındaki yanlış üst seviye launcher
anahtarını activation öncesi yakaladı; yalnız nested scientist pin ve ilgili
policy/launch/argv digestleri düzeltildi. Yanlış hazırlık girdileri korunur,
kullanılmaz; bu hata üçüncü actual runtime hatasının nedeni diye sunulmaz.

Düzeltilmiş private delta karşı review: policy3a72894d…/launch5fc96019…/
argvaf9fa935…/joint9d4e0a7f…/staticbfed1e96…;107 kaynak ve12 config
pinleri eşleşti. API18598 kalan1141,98s, ortak900s koşunun1200s toplam
gereksiniminden kısa: yalnız CPU native-confirm tanısı yapılabilir. Model/task/
Lab execute çağrılmaz; gerçek koşu öncesi ayrı taze API generation gerekir.

Kısa proje-disk TMPDIR ve explicit pytest basetemp ile son zorunlu kapı
2277 passed/7 skipped/121 deselected, yedi araç gerçek exit0. Önceki iki
hatalı test-alanı koşusu korunur; source/UDS/disk guard gevşetilmedi.

### Actual tanı ile bulunan profil kimliği hatası

Dördüncü actual AOS generation5bedf2e68a954646b51d9098e2e87d55, MainPID0/
exit2: maskelenmiş zincir native Bootstrap._bindings:95 deployment kontrolünü
gösterdi. AOS Bonsai Vision constructor manifest pins sözlüğüne recovery ve
vision schema/protocol pinlerini ekler. Scientist registry raw manifest
digestd4a55a3e… kullanıyordu; actual native vision digest a13700c4… farklı.
Decider ham manifest kimliği eşleşir. Guard doğru reddetti; native artifact
receipt üretimi, source/runtime admission veya Desktop/GPU kabulü sayılmaz.
GPU/task/Lab execute çağrısı yok.

Düzeltme Scientist preparer/factory ve profil kimliği hesaplamasına verildi;
AOS kaynağı veya mevcut native manifestler değiştirilmeyecek. Native recovery
ve vision kimlikleri ayrı tutulmalı; raw manifest SHA gate korunmalı.

### Native profil kimliği düzeltmesi

Scientist configured factory0b370a2f… ve preparercb3d5137… ayrı rawmanifest
SHA gate korunarak Bonsai recovery/vision derived deployment hesaplar. AOS
source-pinned RecoveryPlan/VisionScene şema hashleri ve native protocol
pinleri kullanılır; Decider değişmez. AOS CPU constructor readback kimlikleri:
recoveryad66723f… ve visiona13700c4….27 hedefli CPU test, gerçek native
constructor eşleşmesi ve eski raw Bonsai kimliği reddi geçti; Ruff temiz.

AOS source-only review aynı değişimi doğruladı; ortak kontrol sözleşmesi
değişmedi. Özel profiller259caac4…/policy70492453…/static450b7c65…/
launch58453d6a…/argv13f9ece2… ile yeniden pinlendi; raw manifest, ağırlık
ve dependency closure değişmedi. Ortak model/uzun deney kabulü henüz yok.

Dördüncü CPU broker633946/2dbead26… yalnız kendi generation ve canonical
idle/token17/17done request doğrulanarak kapatıldı; PID yokluğu kaydedildi.
Bu broker kapanışı yeni model/GPU release kabulü değildir; yeni GPU çağrısı
olmadı. Eski API lifetime tam araştırma koşusuna yeterli kabul edilmez.

Profil düzeltmesinin zorunlu kapısı2281 passed/7 skipped/121 deselected,
yedi araç gerçek exit0. Hedefli testler fixture ve CPU native kimlik
karşılaştırmasıdır; gerçek ortak GPU kabulü hâlâ çalıştırılmadı.

### Gerçek CPU native açılış ve kapanış kanıtı

Commitfa59b0c ile fifth generation338c6e47ca7f41a3a0e5e13c6523e375/
PID648317 gerçek native confirmation ve Desktop yaratımından geçti.
Authenticated /api/state: AGENT/running, izole network=false container;
receipt aynı actual caller generationa bağlı. AOS static delta karşı
review107 kaynak/12config pinini, iki native kimliği, profile_config_sha
ve policy/static closure eşleşmesini doğruladı.

Deneme sonunda yalnız bu AOS generation ile broker647961/b8aa9bf…
graceful kapatıldı. Original PID/cgroup ve exact container yokluğu,
Application shutdown complete ve canonical idle/token17 kaydedildi.
GPU request sayısı değişmedi:17 historicaldone, yeni model/task veya
Lab execute çağrısı yok. Aktif ana AOS ve Scientist arayüzleri etkilenmedi.

Geçen: kaynak/profil uyumu, gerçek CPU native açılış, authenticated controller,
aynı caller receipt ve owned startup cleanup. Çalıştırılmayan: native GPU
görevi/devir, AOS üzerinden900s Scientist deneyi, AOS sonuç doğrulaması ve
entegre iptal/toparlanma. Bunlar için taze API ve yeni caller receipt gerekir.
Mevcut Scientist inflight stop kanıtı ayrı kalır; yeni CPU kapanışı GPU
release/fairness kabulü değildir.


### İlk gerçek native görev ve kontrol bağlantısı düzeltmesi

Yeni API18599 generation467061d9… ve native caller659445/7da70705…
ile authenticated hello görevi gönderildi. Job7478caab…/run3e148c9f…
11,87s model-call aşamasından sonra failed oldu. Yeni GPU tahsisi yok:
canonical token17 idle,17 tarihsel done request; Lab execute çağrılmadı.
Bunlar gerçek başarısız görev kanıtıdır; model/GPU kabulü değildir.

İlk incelemede infer intent tablolarının boşluğu bootstrap intent yokluğu
olarak yorumlanmıştı. Ayrı desktop_events kalıcı bootstrap audit bulundu:
control56ee84d3…/requesta0a1d2bf…,13:03:50.787316UTC. Audit korunur;
audit kaydı tahsis, terminal sonuç veya release kanıtı değildir.

AOS istemcisi soketi açtıktan sonra ağır current/source ve durable intent
kontrolleri yapıyordu; broker accepted frame penceresi5s bu sırada tüketildi.
AOS kendi kaynağında actual SO_PEERCRED probe→close→durable hazırlık→fresh
socket/full peer equality→son authorization→single-frame sırasını uyguladı.
10s genel deadline,5s frame,owner/generation,source ve replay kontrolleri
korundu. Gerçek CPU UDS gecikme regresyonu önce RED; düzeltmeden sonra20
EvidenceClient ve51 bootstrap/desktop testi geçti. Paket doğrulaması5287
kontrol; model/runtime/eğitim kabulü değildir. Yeni source63b16dc7d4… ve
EvidenceClient92a4978b… sonraki özgün caller generation için pinlenecek.

Başarısız kendi native caller659445/7da70705… ve broker654927/f6a83ff6…
kapatıldı. Original PID/cgroup ve exact container yokluğu kaydedildi;
bootstrap audit ve task failed sonucu korundu. Cleanup GPU release kabulü
sayılmaz çünkü yeni tahsis yapılmadı. Eski API de artık terminal; receipt
ve API generation yeniden kullanılmaz. Aktif ana AOS oturumuna dokunulmadı.


### Fresh boot ile ikinci gerçek görev

Host boot3f14059f… için eski9a4… runtime kanıtları reddedildi. Yeni
API18600/4782/494038f1… current ve authenticated capability eşleşmesi
AOS tarafından bağımsız doğrulandı. Native source63b16dc7d4…,
policy731da812…/staticd89f5d0e…/launch35766ac1…/jointabab053d…/
argvfedd7b0b…;107 actualsource pin eşleşti, yalnız EvidenceClient değişti.
Models/profiles259caac4… değişmedi. Sözleşme control.v1/evidence.v2
ve mevcut profile-output.v2; retainedprovider FD verilmediği için retainedv3
kabulü açık kalır.

Native5117/a72513ff…/broker5105/4b525b1f… ile hellojobec919288…/
run27b9442d…16:29:35.357963UTC gönderildi. Receipt kalan293,87s;
system1 error16,439s, görev failed. Yeni GPU allocation yine yok:
token17idle/17historicaldone. Kalıcı bootstrap controlc0e9510c…/
requestb0ab470b…16:29:42.925611UTC auditi korunur. İlk soket sırası
regresyonunun CPU'da düzelmesi gerçek ortak yolun çalıştığı anlamına gelmez.
İkinci failure exact exception yolunu AOS kendi kaynağında sır/message/path
ve request body içermeyen sınırlı tanıyla görünür kılacak; kör replay yok.

Ownednative/broker originalPID,cgroup ve exactcontainer1ab5eca8… yokluğu
ve Application shutdown complete doğrulandı. Readonly GPU örnekleri yalnız
KWin ve46MiB gösterdi; bu model VRAM peak ölçümü değildir. ScientistAPI
Labexecute gönderilmedi. CPU/selfservice arayüzü ayrı yeniden başlatıldı;
aserdargun100.127.36.77 için Tailscale100.127.72.2:8788 bridge açıldı,
loopback overview'da connected=true ve development progress doğrulandı.

Geçen: freshboot/source/policy/currentAPI uyumu, actual CPU açılış,
71 hedefli AOS CPU testi, owned cleanup ve arayüz live progress.
Kalan: exact kontrol failure düzeltmesi ve native GPU görevi/devir.
Çalıştırılmayan: AOS→Scientist900s araştırma/bağımsız puanlama→AOS
rapor SHA doğrulama/readback; entegre iptal/toparlanma/fairness. Önceki
Scientist gerçek inflight-stop kanıtı korunur, bu kabulün yerine geçmez.


### Sınırlı diagnostic görevin gerçek sonucu

AOS kendi istemcisine yalnız bounded aşama/type/function/line tanısını
ekledi: client9a1243bc…,72 CPU testi8,274s PASS. Scientist tüm107
actual source pinini yeniden doğruladı; API18600 current/authenticated
capability eşit,2734s lifetime kaldı. Deadline/frame/source/fencing/replay
kuralları değiştirilmedi. Önceki audit/control kimliği tekrar kullanılmadı.

Yeni native9744/da3ddada…/broker9731/75b06f75… ile tek diagnostic
hellojobb02c8ad8…16:40:53.312898UTC gönderildi. System1 error16,337s;
bootstrap audit16:41:00.730878UTC, taskfailed16:41:09.724356UTC.
Canonical token17 idle; yeni allocation yok. Beklenen exception warning
actual journalda görünmedi. Bu yokluk tek başına exception'un exchange
haricinde olduğunu kanıtlamaz; logging konfigürasyonu veya bootstrap
üst/alt sınırında başka hata mümkündür. AOS gerçek S1/bootstrap catch
sınırında güvenilir bounded tanıyı kendi kaynak sahipliğiyle ele alır.

Ownednative/broker originalPID/cgroup ve exactcontainer yokluğu doğrulandı;
özgün task/audit/HTTP/NVIDIA gözlemleri korunur. Yeni gerçek deneme yalnız
somut tanı/düzeltme ve taze generation/pin kontrolüyle yapılır. Bu delivery
ortak GPU kabulünü, bağımsız araştırmayı veya öğrenilmiş adaptörü kapatmaz.


### S1 boundary ile somut gerçek hata

AOS doğrudan stderr S1 decide tanısını ekledi;32 CPU testi3,689s ve
5287 paket kontrolü PASS. Finalsource63 08900464…/tracked b34aa064…,
report2fc1469f…; decision36e97980… ve SDKclient9a1243bc….107 actualsource
pin eşleşti. Yeni policy597ccf74…/static1610d487…/launchfc49adf5…/
argvf00d3d66…; models ve profile pins değişmedi. OriginalAPI18600
4782/494038f1… current,1831s lifetime kaldı; deadline uzatılmadı.

Native13525/21750528… ile tek hellojobe3eafdec… gönderildi;19,33s
observation sonunda failed. Actual stderr zinciri: decide39→_decide65→
infer119→run107→prepare_infer154→prepare_async334→_complete_prepare282→
_verified_capture255. Kaynak satır numarası bağımsız doğrulandı:255 guard()
çağrısıdır.249 bağımsız expected binding verifier ve253 full current check
geçilmiştir. İlk yorumdaki binding mismatch çıkarımı düzeltildi; reddeden
özgün async hazırlık deadline/cancel guard'dır. Kontrollü koşuda iptal isteği
gönderilmedi.10s bütçede tekrarlı source/current check maliyeti incelenir;
blind timeout artırımı, permissivecache veya guard kaldırımı yapılmaz.

Original native/broker PID,cgroup ve exactcontainer yokluğu doğrulandı;
canonical idle/token17 ve yeni allocation0. Soket cevabı almak model,
release veya bağımsız araştırma kabulü değildir. AOS kendi check scheduling
maliyetini; Scientist kendi native receipt snapshot/source CPU maliyetini
salt okunur paralel inceler. Yeni GPU görevi somut düzeltmeden sonra.


### Ölçülen maliyet ve Scientist küçük optimizasyonu

Salt okunur CPU ölçümü, gerçek native receipt snapshot0,1548–0,1675s;
197 dependency subprocess median0,07825s gösterdi. AOS guarded107source+
12config+policy çift sweep median0,008232s; kaynak dosyası taraması ana
maliyet değildir. AOS hazırlıkta en az10 full current check,30 runtime
verification ve60 receipt snapshot gözledi. Bu ölçümler eski terminal
caller için current admission kanıtı değildir.

Scientist receipt producerb94ec8c4… metadata'yı dağıtım başına bir kez okur.
Standart Distribution.version aynı metadata['Version'] alanıdır; override
eden provider için özgün version getter korunur. Discovery sırası,
normalization,duplicate last-wins ve missingVersion reddi değişmedi.
Before/after snapshot,197 exactversion map,subprocess -B/-I ve2s timeout,
source/generation/physical identity/deadline/revocation gate korunur.
Actual native interpreter197map eşit; ölçülen tek readback0,05834s.
Yaklaşık60snapshot için1,2s kazanç tahminidir, gerçek kabul ölçümü değildir.

23 targeted receipt CPU testi ve zorunlu kapı2283 passed/7 skipped/
121 deselected/52warnings; yedi araç gerçek exit0. Dependent private source
ve policy pinleri bu producer ile yenilenecek; eski28713778… pin reddi doğru.
Yeni freshAPI18601/24266/84447617…3600s original lifetime ile açıldı.
Eski owned18600/4782/494038f1… PID/cgroup yokluğu doğrulanarak kapatıldı;
uzatma veya eski receipt/generation yeniden kullanımı yapılmadı.

Ortak internal phase kararı: kontrol socket exchange10s/serverframe5s
korunur; response decode sonrası capture verification mevcut original
outer infer absolute deadline altında kalır. Yeni/rebased deadline yok;
capability60s expiry,current/cancel,preconsume transaction fence ve worker
terminal olmadan lock bırakmama kuralları korunur. Outer deadline yoksa
local10s tüm hazırlığa uygulanır. AOS bu değişimin source sahibi; CPU
expiry/cancel/source/generation regresyonu ve finalfreeze beklenir.
Wire contracts veya GPU tahsis otoritesi değişmez. Native/GPU kabul açık.


### Phase düzeltmesi sonrası gerçek infer sınırı

AOS final phase source98416579…/trackedbda77839…/report7c077b2f…,
bootstrap2f0e2d6c…;70 CPU testi7,916s ve paket5287PASS. Scientist
knownproducerb94… +bootstrap phase delta policy86558099…/staticf72b2684…/
launchae538037…/joint7eb20d24…/argve67101f0… ile pinlendi. İlk private
hazırlık eski jointextra producer287… pininde doğru reddetti; bilinen b94
pin o alanda da uygulanıp immutable partial eşitliğiyle hazırlık tamamlandı.
Erken ready mesajı aynı kanalda düzeltildi; bu preparation failure runtime
başarısızlığı veya native admission diye sunulmaz.

AOS bağımsız review17:12:38UTC: record36a4af93…,107source/12config,
actualAPI18601/24266/84447617… before/after current,normalized token hash,
authenticated exact capability eşit;3103,75s lifetime kaldı. Native caller
26027/533f37e7…/broker26010/a6bdfc14… freshreceipt295,69s ile tek
hellojob56ee0568…/runa5c2f03a… başlattı. Terminal failed; system1error
23,256s, observation26,139s. Yeni actualtrace ScientistUncertainTurn→
BrokenPipeError at scientist_transport.infer359 sendall. Bootstrap phase
geçti: scientist_turn_intents1/admission_history1 (öncekiler0). Bu gerçek
kontrol/admission ilerlemesidir; model veya GPU kabulü değildir.

Originalrequest3ccf9376… statepending/deadline4669,851917228;
requestSHA2bfe51e9… ve generation26010/a6bdfc14… korunur. BrokenPipe
sonrasında kaç byte gittiği varsayılmaz; replay,delete,reset veya yeni
caller ile eski intent adopt yapılmaz. AOS TurnClient de socket açıldıktan
sonra heavy durable/current kontrolleri yapıyor; actual peer probe/close
ile hazırlık→fresh authenticated dispatch sırası ownsource'ta ele alınır.

Owned native/broker originalPID,cgroup ve exactcontainer yokluğu doğrulandı.
Canonical token17idle/17historicaldone/newallocation0; sadeceKWin VRAM
gözlendi. Bu CPUphysicalcleanup, pending intent terminal kararı veya GPU
release kanıtı değildir. Scientist Lab900s execute/puanlama/AOSreport
readback çağrılmadı. Unknownintent ve originalcapture kanıtı saklanır;
sourcefix+freshgeneration sonrası kontrollü yeni istek ayrı kimlik kullanır.


### Never-received admission uzlaştırma açığı

Canonical SQLite readonly sorgu: original3ccf9376… için
control_requests0/gpuqueue0/childbindings0; fiziksel eski caller/broker
PID,cgroup ve exactcontainer yokluğu kanıtları ayrıca korunur. Mevcut
ControlStore.no_admission satır yoksa return eder; retained/physical
snapshot originalrow olmadan unauthorized döner. Dolayısıyla mevcut
accepted reconcile yüzeyinden bu istek için terminal receipt çıkmaz.
AOS pending+capture, GPUidle veya source audit ile resolved diye yazılmaz.

AOS TurnClient stage düzeltmesi probe→close→durable/admit→fresh actualpeer
equality→last typed authorization/current/socketinode→singleframe sırasını
korur.85 CPU testi9,091s geçti; selected94f89603…/transport9523892b…
final report henüz beklenir. Bu Scientist tarafından actual native
koşturulmadı. Originalpending3cc için kabul edilmiş trustedresolution
olmadan yeni native oturumu/DB ile bypass yapılmaz. Sahte admissionrow,
cleanupgrant,reservation veya budget yazısı oluşturulmaz. Dar no-admission
observational terminal sözleşmesi iki oturumda değerlendirilecek; tek
canonical GPU authority ve fencing korunur. Genel UI/CPU laboratuvarı
bu scoped integration engelinden bağımsız açıktır.
