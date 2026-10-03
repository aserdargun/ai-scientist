# M0 kabul kaydı

## Son CPU teslimi — 2026-10-03, koşular arası seçilmiş bulgu aktarımı

`3873c13b-66fb-475c-916e-d2f28633bc2c` completed: önceki üç LSH/OPTICS/SOM
bulgusu yeni öneri bağlamına taşındı; 9 baseline + 1 bağımsız LSH ölçümü,
222,87 saniye. Yeni aday DISCARD; iyileşme veya eğitim iddiası yok.
İdempotent tekrar, eski raporların korunması, gerçek masaüstü/mobil okuma ve
Director/10 Scorer/sandbox kapanışı doğrulandı. **3305 test geçti, yedi
kalite komutu exit 0**. [Kullanım ve kanıt](119-prior-findings-context.md).
Bu, açık seçimle bağlam aktarımıdır; otomatik öğrenme/skill terfisi, gerçek
öğretmen ve AOS GPU paylaşım/iptal kabulü açık kalır. Native MAIN değişikliği
ve AOS birlikte çalışma ayrı inceleme yolundadır; merge/push/deploy yapılmadı.

## Son gerçek yerel araştırma — 2026-10-02

`e5ec820a-8abb-4100-ba89-3f210209c6ed`: **6/6 öneri, S1×2/S2×4,
LSH×3/SOM×2/OPTICS×1**, 9 başlangıç + 6 bağımsız aday ölçümü, SQL completed.
Admission→terminal **934,062 s**, gerçek **8278 token**, tepe VRAM **12890 MiB**.
Altı karar DISCARD; bu sentetik snapshot'ta iyileşme yok. 109 checkpoint,
15 Scorer artefaktı ve altı kararın replay'i doğrulandı; altı GPU tahsisi ve
geçici süreç/DB temiz. Replay'in özel artefakt kökü hatası ayrı ürün dalında
düzeltildi; ana native kaynak `55c5300` korunur. AOS, public araştırma,
holdout ve tüm M0 kabulü hâlâ açıktır. [Rapor ve sınırlar](118-six-proposal-local-research.md).

## Son CPU ürün teslimi — 2026-10-02, PostgreSQL OMR akışı

Ayrı `feat/omr-stream-v1` dalında owner kapsamlı PostgreSQL seçimi uzun CPU
akışına bağlandı. **192 eğitim + 4224 izleme satırı / 66 parça / 227,58 s**;
gerçek UTC, kesirli örnekler, zaman boşlukları ve aynı fit korunarak
bağımsız `completed` tanı raporu üretildi. Gerçek HTTP iptali sonrası etkin
kaynak sorgusu/bağlantısı **0,174 s** içinde kapandı. Özgün timeout, yabancı
owner, idempotency, eş zaman/fazla satır reddi geçti; geçici işçiler ve DB temiz.

Veri PostgreSQL'de üretilmiş fixture'dır; endüstriyel araştırma doğruluğu
veya öğrenilmiş adaptör değildir. Son kalite kapısı: **3198 passed, 7 skipped,
177 GPU/live deselected; yedi komut exit 0**.
[Kapsam ve kanıt](117-bounded-omr-stream.md).
Ana `55c5300` ve AOS kaynak/config pinleri korunur; yeni özellik ayrı yerel
daldadır, merge/push/deploy yapılmadı. Tarayıcıda görsel kabul, gerçek
crash/resume, endüstriyel kaynak ve native AOS/GPU kabulü açık.

## Son ek — 2026-10-02, gerçek kabul için belge süresi

Yaklaşık 300 saniyelik CPU baseline'ın ikinci AOS GPU çağrısından önce
belgeyi tüketmesi düzeltildi. Yeni launch girdisi tam sayı 1–900 saniye
seçebilir; varsayılan 300, üst sınır özgün pinli yetkinin bitişidir.
Eski belge/yetki yenilenmez. **61 hedefli kontrol ve tam kapıda 3033 test
geçti; yedi komut exit 0.** [Kanıt](review-evidence/native-artifact-window-20261002.json).
Yeni kaynak/config paketiyle başarılı araştırma sırasında gerçek queued
GPU devri, ardından ayrı deneyde inflight iptal ve fiziksel cleanup
[planı](116-coordinated-acceptance-window.md) hazırdır. AOS karşı incelemesi
ve yeni gerçek GPU koşusu hâlâ açıktır; aşağıdaki 3002 testlik kayıt önceki
kaynak sürümünün tarihsel kanıtıdır.

## Güncel sonuç — 2026-10-02, retained yaşam döngüsü düzeltmesi

İlk control düzeltmesinden sonra provider hazırlığı ve tüm resolution
zincirinin de özgün süreyi aştığı bulundu ve düzeltildi. Gerçek AOS
interpreteri üzerinde **CPU fixture** bileşimi: 106 doğrulama, 636 taze
snapshot ve 16 native Codec; **23,916 saniye / 30 saniye sınırı**, exit0.
İç control/provider sınırları 3 saniye, metadata isteği 2 saniye kalır;
yetki, fencing, iptal ve cleanup denetimleri korunur. **88 odaklı kontrol
ve tam kalite kapısında 3002 test geçti; yedi komut exit0.**
[Son kaynak, ölçüm ve kapı kanıtı](review-evidence/native-retained-lifecycle-scope-20261002.json).

Yeni APIv6 süiti ve AOS v8'in boş alanı dosya düzeyinde hazırdır. Son inceleme,
300 saniyelik artifact belgesinin yaklaşık 300 saniyelik CPU baseline'dan
sonra çekişmeli GPU kabulüne yetmediğini gösterdi. Açıkça seçilen, özgün
yetkiye kırpılan yeni belge süresi ve iki ayrı deney için
[kabul koşusu planı](116-coordinated-acceptance-window.md) hazırdır;
bu değişiklik yukarıdaki yeni 3033 testlik kapıyla doğrulandı. Yeni
servis/token/generation/yetki saati veya GPU koşusu başlatılmadı. Kaynak/config
eşleşmesi, gerçek retained resolution ve ikinci AOS görevi, çekişmeli adil
GPU ilerlemesi, ayrı kontrollü iptal/toparlanma ve aynı koşunun bağımsız
rapor/kapanış kanıtı **açıktır**. Doğrudan oturum mesaj kanalı erişilemiyor;
yeni kaynaklara AOS onayı varmış gibi davranılmaz. Lisans ve genel CI
işleri bu runtime kabulünden ayrıdır.

### Son gerçek koşu ve önceki control düzeltmesi

[Son gerçek v7 denemesi](review-evidence/native-v7-retained-acceptance-failed-20261002.json):
native açılış ve fence25 gerçek AOS çıkarımı geçti; ilk AOS görevi retained
`post_intent_authorization` aşamasında başarısız oldu, pipeline exit1.
Scientist v5 deneyi, ikinci AOS görevi ve rapor doğrulaması başlamadı.
Modelin fiziksel drain/release kanıtı, kendi native servislerinin ve
masaüstünün kapanışı, API MainPID0 ve korunan clone DB'nin exit0 durması
doğrulandı. Arayüz/tünel açık. Tek control içindeki tekrarlı doğrulamaların
maliyeti azaltıldı: tam CPU fixture zinciri özgün üç saniyelik sınırda
2,729 saniyede tamamlandı. 83 odaklı kontrol ve son tam kalite kapısı
2997 test/yedi komut exit0 geçti. Bu kaynak ve CPU teslimidir; yeni gerçek
retained resolution, adil birlikte çalışma ve kontrollü iptal açıktır.
[Son kaynak/CPU teslimi](review-evidence/native-retained-control-scope-20261002.json).
[Ayrıntı, ölçümler ve AOS aktarımı](115-native-retained-resolution-wiring.md).

### Bu denemeden önce tamamlanan kaynak ve hazırlık

[Tekrarlı çağrı için kaynak teslimi](115-native-retained-resolution-wiring.md):
retained factory, özgün aktif request/deadline ve bağımsız proof bağlantısı
kaynakta hazır; AOS kaynak incelemesi uyumlu. Final imajla yedi kalite komutu
exit0/2959 test geçti. Bu tarihsel kapı son performans düzeltmesinin kabulü
değildir. Eski APIv4 süresi uzatılmadı. Yeni APIv5 gerçek exit0 ile
başladı; generation/capability ve altı eski deneyin değişmediği doğrulandı.
Native paket önce disabled olarak üretildi, karşı inceleme ardından 141
kaynak piniyle enabled yapıldı ve yukarıdaki gerçek v7 denemesinde kullanıldı.
[CPU hazırlığı ve açık sınırlar](review-evidence/api-v5-native-preparation-20261002.json).

[İlk uçtan uca rapor](114-first-native-research-report.md): gerçek AOS model
çağrısı ve Scientist tek sentetik snapshot araştırması completed;
AOS rapor save/readback eşliği, canonical23/24done ve fiziksel model/işçi
kapanışı doğrulandı. İkinci AOS admission bağımsız resolution eksikliğiyle
engellendi; fairness ve kontrollü iptal açık. Bu koşu aşağıdaki geniş M0,
public veri, çok önerili araştırma ve eğitim kabullerini tamamlamaz.


**2026-10-02 önceki native kabul gözlemi:** İlk AOS hello görevi gerçek model,
başarılı trace ve tam dosya içeriğiyle geçti; AOS oturumu bağımsız doğruladı.
AOS kontrol sözleşmesiyle açılan `6dd84567` Scientist araştırması bir öneri ve
bağımsız primary seed0 ölçümü üretti. Veri sentetik çalışma modlarıdır;
bu sonuç Public27 araştırma veya tamamlanmış rapor değildir.
Exception kapanışı `stop_requested` yazmış fakat ilk stop event kaydı eksik
kalmıştır; recovery `first_stop_deadline_unavailable` nedeniyle ilerlemedi.
Özgün süre doldu, eski run/deadline yenilenmedi. Director ve model child
MainPID0; eski PID/cgroup yok, canonical Lab request done/token22;
model child gözlem tepesi12760MiB. Terminal araştırma raporu/AOS readback,
adil eşzamanlı ilerleme ve ayrı kontrollü iptal kabulü açıktır.
İleriye dönük artifact yolu ve atomik/fenced stop-event düzeltmesi uygulandı;
11 artifact regresyonu ve14 gerçek PostgreSQL rol kontrolü geçti. Son
kaynak kapısı2854test/tüm7komut exit0; AOS salt okunur karşı incelemesi geçti. Yeni
GPU/araştırma kabulü henüz çalıştırılmadı.
[Düzeltme ve sıradaki kabul](113-native-research-closure-fix.md).
[İlk native başarı ve eksik araştırma kapanışı](review-evidence/native-aos-partial-acceptance-20261002.json).

**2026-10-02 gerçek native AOS model çağrısı, başarısız foreground görevi:**
Gerçek Decider `system1` çağrısı `ok`; trace gecikmesi52,750s (kuyruk/yükleme
dahil, saf inference süresi değil). Aynı hello işi POST observer timeout
sonrası salt okunur gözlemle bulundu; ikinci POST veya restart yapılmadı.
Root operatör hatası ve özgün hello onay süresinin kaçırılması nedeniyle görev
`failed`; başarılı foreground veya araştırma kabulü verilmez. Ortak scheduler
tek AOS isteğini fiziksel drain kanıtıyla tamamladı: cgroup boş/GPU yok,
late-start fencing, `release_outcome=released`. Root aynı model worker'ın
MainPID0/cgroup yok/original PID yok durumunu ayrıca doğruladı. Canonical18done,
VRAM46MiB; worker PyTorch allocated tepesi3635,337MiB, tüm cihaz tepesi
ölçülmedi. Worker yükleme19,559s/inference1,054s ölçtü. Kendi AOS/broker servisleri ve masaüstü normal
cleanup exit0 ile kapandı. Scientist araştırması, AOS rapor doğrulaması ve
ayrı kontrollü iptal çalıştırılmadı. Bir sonraki kapsam için POST belirsizliğini
aynı işe bağlayan ve onayı polling süreci içinde yetiştiren operatör hazırlanıyor.
[Gerçek çağrı, başarısızlık ve fiziksel cleanup kanıtı](review-evidence/native-aos-foreground-failed-20261002.json).

**2026-10-02 gerçek native AOS CPU açılış/kapanışı:** Gerçek checkout ve mevcut
factory ile izole broker, AOS konsolu ve pinned Docker masaüstü açıldı. AOS
süreç doğumundan hazır journal kaydına11,395s; CPU artifact doğrulaması7,342s.
Güncel generation/namespace/cgroup, imaj/mount/lifecycle ve HTML byte eşliği
bağımsız doğrulandı; journal özgün240s startup sınırı içindedir. Stale socket,
geçici exec kimlik okuması ve root302 observer hataları korundu; aynı canlı
generation yeniden gözlendi. Model çağrısı öncesinde gerçek300s artifact
freshness dolduğu için sahip olunan iki servis normal kapatıldı; MainPID0,
boş cgroup ve masaüstü kaldırımı geçti. Canonical17done değişmedi; yeni GPU
isteği0. Bu gerçek CPU entegrasyonudur; model/GPU kabulü ve araştırma açık.
[Açılış, hatalar ve bağımsız cleanup kanıtı](review-evidence/native-aos-cpu-startup-cleanup-20261002.json).

**2026-10-02 güncel ortak runtime hazırlığı:** Mevcut ortak GPU slice'ın RAM
16 GiB/swap0/CPU200%/128 task limitleri kernel üzerinde doğrulandı. Yeni izole
API18602 gerçek exit0 ile açıldı; taze principal/generation, üretim capability
GET eşliği ve sentetik mode kaydının dar Scorer RPC/Planner bağlantısı geçti.
AOS gerçek PID/generation/cgroup ve 1 deney/900s/30000token bütçesini bağımsız
doğruladı. Scheduler salt okunur kontrolde17done/0active/0queued; rezervasyon
alınmadı. Model/broker/GPU/deney/dispatcher başlamadı. Tam native yapılandırma
incelemesi, gerçek birlikte çalışma ve kontrollü iptal kabulü açık. Aşağıdaki
tarihli kayıtların henüz çalıştırılmadı ifadeleri kendi gözlem anına aittir.
[Doğrudan koordinasyon ve kanıt hash'leri](06-aos-coordination.md).

**2026-10-02 gerçek native boş envanter toparlanması:** Yeni `c960933b` koşusu
gerçek owner/27profil/81plan ile stop istedi. Hazırlık observer'ı yanlış Director
rolüyle sayım yaptığı için exit1 verdi; hata korundu, yetkiler genişletilmedi.
Normal recovery özgün120s içinde exit0 ile `stopped` ve hash-doğrulanmış rapor
üretti:81unattempted/0score/0job/0drain, aynı native worker için gerçek
register→seal→retire. Root üç unit MainPID0/cgroup boşluğunu bağımsız doğruladı.
ClonePID0 ve eski iki expired run değişmedi. Bu gerçek CPU cleanup kanıtıdır;
public veri ölçümü veya ortak GPU kabulü değildir.
[Native kanıt](review-evidence/empty-baseline-stop041-native.json).

**2026-10-02 güncel boş deney stop düzeltmesi:** 0041 additive
register/seal/retire kanıt yolu uygulandı ve bağımsız incelendi. 163 odaklı CPU
testi ve ayrı PostgreSQL veritabanında 14 gerçek rol kontrolü geçti. Original
deadline, owner/generation, nonempty plan ve mevcut job kapanış yolu korunur.
Offline güncel image build/parity exit0; yeni kaynak kapısı2853test/tüm7komutexit0.
SQL fixture'ları gerçek süreç temizliği değildir: yeni native stop/recovery,
Public27 ölçümü ve ortak GPU kabulü henüz çalıştırılmadı. Eski iki expired run
değiştirilmedi. AOS yeni disabled paketin 118 kaynak hash'ini doğruladı ve kendi
izole workspace'ini hazırladı; eski yetkiler yeni koşuda kullanılmayacak.
[SQL kanıtı](review-evidence/empty-baseline-stop041-sql.json).

**2026-10-02 güncel gerçek kapanış:** V5 tek consumer exit0 ile immutable
retained no-admission closure kaydı oluşturdu; Scientist witness exit0 ve iki
oturumun bağımsız schema28/readback/süreç temizliği kontrolleri geçti. Eski
intent/kanıt/deadline değişmedi. Bu kapanış engeli çözüldü; gerçek GPU devri,
adil AOS birlikte çalışma ve Public27 araştırma/holdout kabulleri hâlâ açık.
Son kaynak kapısı2833test/tüm7komutexit0. İlk score job öncesi duran81planlı
native baseline'ın boş iş envanteri kapanış dalı gerçek koşuda reddedildi;
kanıta dayalı düzeltmesi inceleniyor, CPU ölçümü tamamlanmadı.
[Kapanış kanıtı](112-aos-retained-closure-coordination.md),
[Public27 açık iş](110-public-local-qwen-preparation.md).

**2026-10-01 gerçek cleanup envanter düzeltmesi:**
Tek observer denemesi proof/mint öncesi exit2; eski request/DB27 korundu. Gerçek
10. gpu_runtime_bindings surface eksikliği atomic/provider/AOS okuyucularında
düzeltildi;2519CPU/yedi-komut kapı exit0. V3source76 readonly hazırlığı geçti;
gerçek closure ve GPU/AOS kabulü henüz gerçekleşmedi, sayılar değişmedi.
[Hata, düzeltme ve kalan gerçek kabul](111-runtime-binding-cleanup-fix.md).

**2026-10-01 gerçek public araştırma hazırlığı:**
Mevcut research-authorized public27 manifesti, ayrı local-Qwen registry/6 öneri
ve principal ile hazırlandı; mevcut dosya/schema okuyucuları ve root bağımsız
kontrolü exit0. DB/Planner/ölçülmüş holdout güncel readback, model/admission
çalışmadı. Tarihsel135 proof güncel araştırma kabulü değildir; sayılar değişmedi.
[Tamamlanan hazırlık ve kalan kapılar](110-public-local-qwen-preparation.md).

**2026-10-01 izole Scorer deployment:** Credential pathname bütün alt süreç ve
consumer'lara taşındı; geçersiz explicit seçim default ledger'a düşmez. Gerçek
systemd Scorer worker yeni Postgres'e profile yazdı; ana DB unchanged, worker ve
konteyner cleanup doğrulandı. Bu routing kanıtı model puanlaması/GPU araştırması
değildir; M0 sayıları değişmedi. [Teslim ve kanıt sınırları](102-scorer-deployment-isolation.md).

**2026-10-01 gerçek araştırma başlangıcı:** Director dispatch/resume, ortak GPU
slice limitlerini servis başlatmadan doğrulayacak şekilde düzeltildi. Canlı
slice'ın dört aggregate limiti infinity; mevcut ortak kaynak değiştirilmedi.
Ana ledger'daki owner kanıtı olmayan running/stop_requested koşulara dokunulmadı.
Scientist-only local-Qwen yolu AOS desktop engelinden bağımsızdır; gerçek yeni
araştırma ve AOS birlikte çalışma kabulü henüz gerçekleşmedi.
[Güncel yürütme yolu ve engeller](101-native-runtime-admission.md).

**2026-10-01 native runtime yetkisi:** Eski readback'i benimsemeyen bounded native
artifact receipt/current-rights sağlayıcısı ve ACK'ten bağımsız actual service
binding capture kodu eklendi.54 fixture kontrolü; kalite2057 passed/yedi komut
exit0. Yeni helper/CLI gerçek runtime ile henüz çalıştırılmadı. Native AOS görevi
AOS data workspace istiyor; kullanıcı salt okunur sınırı için yanıt bekleniyor.
Gerçek GPU/araştırma/Scorer/AOS kabulü açık; sayılar değişmedi.
[Geçen, kalan ve AOS beklentisi](101-native-runtime-admission.md).

**2026-10-01 native60 ve gerçek model dosyaları:** AOS configured-source verifier
ve native bootstrap factory bağlantısı hazır; dört desktop hook'u ve manifest
deployment digest uyumu düzeltildi. Yanlış uygulama Python ortamı dependency
kontrolünde reddedildi; mevcut doğru model ortamıyla Decider/Bonsai artifact ve
native dependency kontrolleri exit0/8,300 saniyede geçti. Model/GPU başlatılmadı;
Bonsai projection pin doğrulanmış değil. Güncel protocol/lab baseline ayrımı
incelendi ve native60 disabled config üretildi.137 odaklı kontrol; zorunlu kalite
2024 passed/yedi komut exit0. Gerçek launcher/current authority/rezervasyon ve
uçtan uca GPU araştırma kabulü açık; **11 geçti/7 kısmi/4 açık** değişmedi.
[Geçen, kalan ve AOS aktarımı](100-configured-source-model-readback.md).

**2026-10-01 AOS native factory/provider:** Actual ayrı AOS servisinde kendi
native factory durable before-send audit yazdı; Scientist bağımsız SQLite ile
cevap öncesi doğruladı.3 control/20 native provider isteği, durable resolution,
pause/generation reddi ve iki original process absence geçti; exit0/12,924 saniye.
Runtime/profile/clock sentetik CPU; model/GPU/Scorer çalışmadı. Yeni configured
source/current runtime ve gerçek GPU kabulü açık, kabul sayıları değişmedi.
[Geçen/kalan/aktarılacaklar](99-native-aos-bootstrap-provider.md).

**2026-10-01 native bootstrap/config bağlantısı:** Actual AOS native58 profili,
async bootstrap API ve aynı Controller/Store history2.0 factory uygulanıp
doğrulandı. Üç mevcut yerel çalışma profili ve disabled policy gerçek manifestlerle
üretildi.133 odaklı kontrol geçti; actual AOS interpreter API kontrolü sentetik
CPU runtime kullandı. Gerçek expected/current authority, durable writer ve
native launcher bağlantısı açık; model/GPU/Scorer çalıştırılmadı. Kabul sayısı
değişmedi. [Uygulama, sınırlar ve AOS aktarımı](98-native-bootstrap-configuration.md).

**2026-10-01 gerçek servis bağlantısı:** FD0 inherited SOCK_SEQPACKET provider
ayrı actual AOS ve Scientist systemd servisleriyle çalıştı.20 doğrulanmış provider
isteği, durable resolution/fresh admission, unchanged private Store ve iki özgün
sürecin yokluğu kanıtlandı; parent exit0/11,206 saniye. Session/admission yetkisi
sentetik; GPU/model/Scorer çalıştırılmadı. Gerçek config/principal wiring ve GPU
kabulü açık; kabul sayısı değişmedi.
[Geçen, kalan ve aktarım](97-real-service-provider-bootstrap.md).

**2026-10-01 iki interpreter arasında bağımsız provider:** AOS açık resolution
transaction'ı sırasında aynı broker control nesnesinden bağımsız budget ve
no-admission snapshot okuyabiliyor. Existing v3 socket → durable resolution →
fresh admission CPU birleşimi actual AOS ortamında exit0/3,318 saniye;81 odaklı
kontrol geçti. Unit/session authority sentetik, model/GPU/Scorer yok. Canlı
FD bootstrap ve gerçek current provider/dependency closure hâlâ açık.
Kabul sayısı değişmedi. [Bağlantı, sınırlar ve aktarım](96-authenticated-retained-provider.md).

**2026-10-01 özgün fiziksel readback:** Mevcut retained authority altında tek
query-only transaction ve bağımsız süreç/cgroup/GPU gözlemleri public facade'a
bağlandı. Kaynak pin/drift ve bootstrap yetkisi reddedilir; başka lane ilerleyebilir.
77 CPU kontrolü geçti; OS/GPU gözlemleri sentetiktir. Authenticated AOS provider
ve gerçek model/GPU kabulü açık, kabul sayısı değişmedi.
[Uygulama ve AOS beklentileri](95-original-physical-cleanup-readback.md).

**2026-10-01 gerçek worker payload bağı:** `_prepare()` ürettiği payload hash'i
ile broker zarfı hash'inin karıştırılması iki regresyonla kanıtlandı ve düzeltildi.
Zarfın özgün bytes/schema/principal bağı korunur; child kendi canonical payload'ına
bağlanır.29 birleşik CPU kontrolü geçti. Unit/model gözlemleri sentetiktir; gerçek
model/GPU kabulü açık. [Hata, düzeltme ve kanıt](94-worker-payload-envelope-binding.md).
Kabul sayıları değişmedi.

**2026-10-01 trusted source bütçe okuyucu:** Broker retained-target authority
üzerinden özgün Store intent/allocation/readiness kolonları bağımsız okunabiliyor.
Yeni socket op/kota/ID/GPU allocator yok.24 CPU kontrolü geçti; bootstrap infer
capability ve okuma sonrası revoke reddedildi. Ayrı AOS interpreter'a authenticated
source-provider bağlantısı, fiziksel readback ve gerçek model/GPU kabulü açık.
Kabul sayısı değişmedi. [API ve sınırlar](93-trusted-retained-budget-reader.md).

**2026-10-01 iki ortamlı AOS bağlantısı:** Actual AOS resolution54 API'si
Scientist gerçek evidence-v3 socket'ine bağlandı. Ayrı Python ortamlarında
capability/reconcile → immutable ACK → durable resolution → yeni admission
CPU koşusu exit0/1,389 saniye verdi. Eski istek replay reddedildi, original
kayıtlar korundu. Yetki/saat/physical sağlayıcıları sentetiktir; allocation
öncesi iptal kullanıldı. Model, Scorer ve gerçek GPU kabulü açık; canonical
broker/scheduler henüz mevcut değil. Kabul sayıları değişmedi.
[Geçen, kalan ve çalıştırılmayanlar](92-aos-resolution-composition.md).

**2026-10-01 bağımsız özgün bütçe:** Trusted Store read adaptörü terminal
raporunu kullanmadan özgün intent/queue/allocation/readiness sütunlarından
budget witness üretiyor. İlk saatler ve assigned deadlines yenilenmiyor;
quota/ID/DB kaydı değişmiyor. 27 birleşik CPU kontrolü geçti. Ortak evidence
socket şeması korunuyor; AOS trusted witness retention/injection ve journal
resolution hâlâ açık. [Kaynak, sınırlar ve aktarım](88-aos-independent-original-budget.md).
Bu teslim gerçek GPU/AOS kabulünü veya kabul sayısını değiştirmez.

**2026-10-01 authenticated evidence socket:** Özgün v1 target authority'ye
dayalı ayrı evidence-v2 capability/reconcile akışı mevcut socket'e bağlandı.
110 birleşik CPU kontrolü geçti; normal/retired policy ve gerçek socketpair
ile iptal kanıtı okuma/tekrar çalıştı. Üç geç expiry/revoke/ACL hatası önce
regresyonla yakalandı, sonra düzeltildi. Tam şema AOS artifact'iyle aynı;
38 üyelik yeni AOS kaynak profili tanınıyor. AOS dirty/untracked nedeniyle
ön kontrol exit2; native journal resolution ve gerçek GPU kabulü açık.
[Sözleşme, icra ve aktarım](87-aos-evidence-socket-integration.md).
Kabul sayıları değişmedi.

**2026-10-01 kapanış kanıtı teslimi:** Mevcut Store özgün terminal,
allocation/drain/no-admission preimage'larını retained-target authority ve
özgün budget/child/fence zinciriyle okuyabiliyor. Canceled sonuç yayımlanmaz;
geç revoke transaction'ı geri alır. 78 birleşik CPU kontrolü geçti.
Tam v2 şema/bundle adayı paketlendi; v1 socket değiştirilmedi. Gerçek AOS
admission_v2 ön kontrolü dirty/untracked kaynak nedeniyle exit2 verdi.
Evidence transport/journal resolution ve gerçek GPU kabulü açık.
[Kanıt, sınırlar ve AOS aktarımı](86-aos-terminal-evidence-delivery.md).
Bu kaynak teslimi kabul sayısını değiştirmedi.

**2026-10-01 cleanup kapasitesi:** Yeni kabulde özgün süre bütçesine bağlı
control/grant rezervleri aynı SQLite transaction içinde ayrılıyor. Dolu ledger
veya infer cache kabul edilmiş hedefin ayrılan cancel/reconcile payını
tüketemiyor. Sekiz doluluk ve iki geç policy-revoke hatası önce yakalandı;
180 birleşik CPU kontrolü ve 1640 testlik yedi-komut kalite kapısı geçti.
Kota/ID tekrarları süre veya yetki yenilemiyor. Bu kaynak/CPU teslimidir;
caller restart, ortak sözleşme ve gerçek GPU devri açık kalır.
[Kanıt ve sınırlar](85-aos-cleanup-capacity.md). Kabul sayıları değişmedi.

**2026-10-01 kaynak entegrasyonu:** Scientist `a60e9e2` kontrollü AOS
çıktısını gerçek yürütücüye bağladı. Özgün istek/bundle/türetilmiş şema ve
allocation fence'e bağlı worker kimliği persistence/replay öncesinde denetlenir.
Bonsai dış şeması AOS note80 ile aynı; yeni admission'ın beşinci çıktı pini
AOS tarihçe sözleşmesinde henüz kabul edilmedi. 173 birleşik CPU testi;
zorunlu kapı 1618 passed / 7 opt-in skipped / 120 GPU-live deselected,
yedi gerçek exit0. Bu kaynak/CPU kanıtı M0.AOS.5/7 veya native release'i
kapatmaz. [Yürütücü kanıtı](82-aos-profile-output-integration.md),
[AOS aktarım özeti](83-aos-profile-handoff.md).

2026-09-30 öncelik: [güvenli stop ve tek koordineli AOS kabulü](65-coordinated-lifecycle-acceptance.md).
Running-only stop yarışı ana runtime'da üç regresyonla yeniden üretildi;
aday CPU kontrolleri geçti. Genel native SQL kabulü ve gerçek GPU devri
açık; mevcut kabul sayıları değişmedi. AOS salt okunur, deploy/push yok.

Sonraki ayrı55545 native direct denemesi **18 kontrol/exit0** ile gerçek
inflight Scorer stop→normal automatic recovery→hash doğrulanmış `stopped`
raporunu ve CPU süreç/cgroup kapanışını kanıtladı. R5 adayında pozitif native
SQL yolu geçti; diğer native negatifler, üretim geçişi ve gerçek AOS/GPU
kabulü açık. [İcra özeti](review-evidence/automatic-stop036-native-direct-proof.json).
Broker principal replay açığı CPU regresyonuyla düzeltilip42 testle doğrulandı;
mevcut GPU tahsis otoritesi/quarantine korunur. Kabul sayıları değişmedi.

R5 stop kaynakları0036 migration ve regresyonlarıyla çalışma ağacına alındı;
startup Restart politikası yalnız ayrı inceleme yamasıdır.24 yeni broker
protokol/cancellation testi geçti; terminal own fixture üzerinde wrong-role
native RPC42501 ile reddedildi. Birleşik kapı **1317 passed / 7 skipped /
120 deselected**, yedi exit0. Üretim schema/services/GPU ve AOS değişmedi;
pending-row CAS/generation/deadline, ortak capability/cancel ve gerçek
GPU kabulü açık kalır.

Kanıt satırları yalnız icra edilen komut ve saklanan çıktıyla kapatılır. Fixture, gerçek model/veri ve AOS gerçek çalışma sonuçları birbirinden ayrıdır. Başlangıç durumu 2026-09-24.

Güncel kabul özeti: **22 maddenin 11'i geçti, 7'si kısmi, 4'ü açık**; 11 madde henüz kapanmadı. Maddelerin iş yükü eşit değildir; bu oran kodun tamamlanma yüzdesi veya kalan süre tahmini değildir. Ana kalan işler başarılı S2/tam Qwen araştırması ve QLoRA ölçümleri, AOS ile tam araştırma/desktop birlikte çalışma kabulü, public veri araştırma akışı, holdout, kalan güvenlik/strateji uygulaması ve final kalite/PR kapılarıdır.

Bağımsız açık kaynak çekirdek, özel SWAPP+AOS fork'u ve ileride model/skill/
eğitim döngüsü için [ürün planı](44-open-laboratory-roadmap.md) eklendi.
Bu plan yeni yeteneklerin tamamlandığı anlamına gelmez. README'deki
geliştirme süre/token/model bilgileri tarihli araç sayaçlarına dayanır.

**Güncel ana sürüm 0.41.0:** yedi kalite komutu exit 0, **1247 passed /
7 skipped / 120 deselected**. Gerçek tek Scorer işi ve süreç-kimliği RPC/
checkpoint taşıması sentetik girdilerde doğrulandı. İzole ana DB kopyasında
yükseltme/geri alma geçti; gerçek dağıtım 47 tablo / 234.073 mevcut satırı,
bütçe/deadline/nesilleri ve yetkileri korudu. API/konsol/Director aktif,
tünel aynı; konsol sürümü 0.41.0. Kabul sayıları değişmedi.
[Gate/imaj](review-evidence/release041-image-gate-r4-summary.json),
[Scorer süreç kanıtı](review-evidence/release041-worker-process-r3-summary.json),
[Dağıtım](review-evidence/release041-deployment-summary.json).

**2026-09-29 gerçek tamper/restart kabulü:** M0.2 kapandı. Güncel kaynakla
byte eşliği bulunan ayrı test ledger'ında dokuz gerçek baseline skoru ölçüldü.
Harness VERSION, evaluator, imaj pini ve bağımlılık pini ayrı ayrı tek byte
ile değiştirildi: dört sonraki öneri Docker/Scorer admission öncesinde typed
REJECT oldu; dört tam CLI resume çağrısı exit 1 verdi. Özgün deadline,
bütçe ve nesil korundu, bütün kaynaklar geri yüklendi. Test **session 34680 /
exit 0**, 315,62 saniye. Bu, imaj blob'unun fiziksel değişikliği veya başarılı
restart değil; pin/sözleşme değişikliklerinin gerçek üretim yollarında reddidir.
[İcra bağı](review-evidence/tamper057-execution-binding.json),
[kaynak bağlı özet](review-evidence/tamper057-summary.json).

**Önceki ana sürüm 0.39.0:** birleşik gate **1163 passed / 7 skipped /
120 deselected**, yedi gerçek exit 0; 149 imaj kaynak dosyasında byte eşliği.
Gerçek dağıtım **session 5925 / exit 0** ile sekiz dosya ve üç işlevlik
0033 migration uygulandı. 47 tablo / 234.073 satır, bütçeler, deadline ve
generation kayıtları korundu; üç sahipli CPU servisi aktif, tünel kimliği
aynı. Ayrı PostgreSQL R3 rehearsal 37 kontrolle exit 0 verdi; tek mevcut
ölçülmüş incomplete baseline kapsamında katalog geri alma ve gerçek
Migrator yeniden yükseltmesi geçti. Kabul sayıları değişmedi; bilimsel,
GPU/model, holdout, AOS birlikte çalışma ve M0.10 açık kalır.
[Dağıtım özeti](review-evidence/release039-deployment-summary.json),
[Gate ve imaj](review-evidence/release039-gate-image-summary.json),
[kapsam ve başarısız geçmiş](61-baseline-duration-and-stop-recovery.md).

**Önceki ana sürüm 0.38.0:** birleşik gate **1104 passed / 7 skipped /
120 deselected**, yedi exit 0; image byte eşliği ve gerçek clone migration/
geri dönüş geçti. Gerçek dağıtım **session 22136 / exit 0**: 14 dosya,
0032 iki işlev, mevcut ledger/bütçe/deadline satırları korundu, üç sahipli
CPU servisi aktif ve tünel kimliği aynı. İlk canlı denemenin exit 1 ve tam
geri dönüş kanıtı korunur. Kabul sayıları değişmedi.
[Geçiş ayrıntısı](60-release038-candidate.md),
[kaynak ve gate bağı](review-evidence/release038-candidate-binding.json).

**Önceki ana sürüm 0.37.0:** bağımsız syscall gözlemcisi ve sınırlı,
bildirimle uyanan bekleme eklendi. Tam gate **1059 passed / 7 skipped /
117 deselected**, yedi komut exit 0; 144 runtime dosyasında imaj eşliği.
Beş gerçek sandbox testi / 15 aşama geçti, altı konteynerin varsayılan
seccomp ve izolasyon ayarları doğrulandı. Özgün test başlatıcısının cleanup
newline hatası nedeniyle exit 1 sonucu korunur; ayrı strict doğrulayıcı
exit 0 ile aynı altı ID'nin kaldırıldığını doğruladı. Gerçek dağıtım
**session 22962 / exit 0**: on dosya, SQL migration yok, mevcut ledger ve
bütçe hashleri değişmedi; üç Lab CPU servisi aktif, tünel kimliği korundu.
Kabul sayıları değişmedi; geniş güvenlik, gerçek LLM/GPU, public araştırma,
holdout ve AOS birlikte çalışma kanıtları bu teslimatla kapanmaz.
[Kaynak ve icra ayrıntıları](45-sandbox-observer-release.md),
[dağıtım](review-evidence/observer0370-deployment.json).

**Önceki ana sürüm 0.36.3:** kalibrasyonu tamamlanmış, tek önerisi kaydedilmiş
ancak aday işi başlatılmamış koşu için dar stop kapanışı eklendi. Tam kapı
**943 passed / 7 skipped / 105 deselected**, yedi komut exit 0; gerçek
PostgreSQL **40/40**, süresi dolmuş 120 saniyelik kapanış yetkisinin reddi dahil.
143 runtime dosyasında imaj byte eşliği ve 281 gate dosyasının ana kaynakla
eşliği doğrulandı. **Session 19702 / exit 0** ile 13 dosya dağıtıldı;
0031 migration özel DB yedeğinden sonra uygulandı. Mevcut skor ve bütçe
kayıtları değişmedi; yalnız projenin üç servisi yeniden başlatıldı.

Başarısız 053 koşusu `fac65252-e14d-4014-9744-333b88083963`, üretim CLI
recovery yolu ile **session 50705 / exit 0** sonucunda `stopped` oldu.
Üç baseline ve bir **ABANDONED** önerinin dört strict belge çifti doğrulandı;
ölçülmeyen adayın skoru, kararı ve süreleri uydurulmadı. Dokuz eski skor,
bütçe checkpoint hash'leri ve nesil 2 korundu. Rapor SHA-256:
`6191a1ac6cc1697565220d2e86b365c9ea3ebccb9bc15808df281f41ce4e0673`.
Bu kapanış önceki başarısız resume'u başarılı yapmaz; hata kanıtı korunur.
Kabul sayıları ve gerçek GPU/model/public/holdout/AOS açık maddeleri değişmedi.
[İcra ve kaynak bağı](review-evidence/stop055-release-execution-binding.json),
[gate](review-evidence/stop055-gate.json),
[PostgreSQL](review-evidence/stop055-postgres40.json),
[dağıtım](review-evidence/deployment0363.json),
[gerçek kapanış](review-evidence/stop055-actual053-closure.json).
Yayımlanan kopyalarda özel DSN dosyasının hash'i çıkarıldı; özgün kanıt
ve DB/kaynak yedekleri özel çalışma alanında tutuluyor.

**Önceki ana sürüm 0.36.2:** provider tamamlanma süresi, öneriyle aynı
değişmez checkpoint içinde run/ordinal/rezervasyon/boot ve girdi hash'lerine
bağlı kaydediliyor. Devam işlemi bu süreyi bir kez kullanıyor; özgün toplam
run süresi ve son tarihi duraklama boyunca dolmaya devam ediyor. Eksik veya
çelişen eski süre kayıtları açık hata olarak kalıyor; 053'ün başarısızlık
kaydı değiştirilmedi. 19 odaklı CPU kontrolü ve tam kapı **session 34150 /
exit 0**, **926 passed / 7 skipped / 100 deselected**, yedi komut başarılı.
141 runtime dosyası için imaj eşliği; **session 69229 / exit 0** ile yalnız
beş değişmiş dosya dağıtıldı, 278 gate dosyası ana ağaçla eşleşti. SQL
migration veya mevcut DB satırı değişikliği yapılmadı. Gerçek 054
kesinti/devam koşusu tamamlandı: 20 LSH önerisi, 29 gerçek Docker/Scorer
skoru ve 23 strict experiment/trajectory çifti. Bağımsız salt okunur
doğrulama **fd9c1b / exit 0**, **38/38** kontrol: dokuz baseline satırı,
özgün 3600 saniyelik son tarih, nesil 1→2 devri ve API/DB/kanonik rapor
hash eşliği korundu. Devam komutu **session 44060 / exit 0**, 326,198
saniye. Kesinti anında ilk provider bütçesi zaten uzlaştırılmıştı;
eksik uzlaştırma sınırı gerçek koşuda ölçülmedi, 19 CPU regresyonuyla
sınırlıdır. GPU/LLM/AOS ve genel stopped-proposal kapanışı açık kalır.
2026-09-29'da mevcut kurulum yeniden başlatıldı: **session 21249 / exit 0**,
üç sahipli CPU servisi aktif ve koşunun aktif işi yok.
[Terminal kanıt](review-evidence/resume054-terminal-execution-binding.json),
[38 kontrol](review-evidence/resume054-verification.json),
[rapor](review-evidence/resume054-canonical-report.json).
[Gate](review-evidence/resume054-gate-execution-binding.json),
[dağıtım](review-evidence/deployment0362.json).

**Önceki ana sürüm 0.36.1:** dar Director kalibrasyon RPC'siyle stop
kapanışı ve API koşu türü düzeltildi. Gerçek izole PostgreSQL **35/35**,
session **18752 / exit 0**. Tam runtime kapısı **session 21216 / exit 0**:
**879 passed / 7 skipped / 100 deselected**, yedi komut exit 0, strict
mypy 131 kaynak, Pylint 9,26. İmaj eşliği ve 272 dosyanın byte eşliği
doğrulandı; deployment session **72127 / exit 0**, mevcut 0030 migration
korundu. Üç sahipli CPU servisi sağlıklı.

Eski `404d9a45-f1fb-4f91-a842-0edcd44bc1b9` koşusu supported recovery ile
**session 45747 / exit 0** sonucunda `stopped` oldu. Ayrı son kontrol;
API/DB/kanonik rapor hash eşliğini, sıfır ölçülmüş skor ve üç gerçek terminal
sonucu doğruladı. Ölçülmeyen zamanlar `null` kaldı; skor uydurulmadı.
Bu stop kapanışı yeni bir araştırmanın OS kesintisi/devam kanıtı değildir.
Public koşunun arayüzde CPU baseline görünmesi de **session 35353 / exit 0**,
üç tarayıcı kontrolüyle doğrulandı; mevcut rapor değişmedi.
[Gate](review-evidence/stop051-gate-r2-execution-binding.json),
[PG](review-evidence/stop051-pg-r1-execution-binding.json),
[dağıtım](review-evidence/deployment0361.json),
[stop kanıtı](review-evidence/recovery0361-stopped-proof.json),
[tarayıcı](review-evidence/public050-browser-postfix0361-evidence.json).
Kabul sayıları değişmedi.

**Yerel başlatıcı ve README sayaçları dahil birleşik kapı:** session
**49170 / exit 0**, **907 passed / 7 skipped / 100 deselected**; yedi komut
başarılı, 73,456 saniye ve 286 dosya boyunca kaynak eşliği. Başlatıcı
yeniden çağrıldı; API/işçi/arayüzün üç eski PID'si korundu. Soğuk boot testi
henüz yapılmadı. Geliştirme metriği güncellemesinin 10 odaklı testi geçti;
gerçek Goal snapshot'ı ve 14 ilişkili oturumdan README/JSON/tarihçe üretildi.
[Birleşik gate](review-evidence/release052-gate-execution-binding.json),
[başlatıcı kanıtı](review-evidence/release052-launcher-proof.json).

**Gerçek süreç kesintisi/devam denemesi 053 — başarısız:**
`fac65252-e14d-4014-9744-333b88083963` koşusunda dokuz baseline skoru
üretildikten sonra yalnız bu koşunun kimliği doğrulanmış Director süreci
SIGKILL ile kesildi. Üretim resume yolu sahipliği 1. nesilden 2. nesle
devretti; dokuz skor, istek, execution sözleşmesi ve özgün son tarih
değişmedi. Ancak devam işlemi **session 61111 / exit 1** ile
`stop_requested` durumuna geçti; terminal rapor üretmedi.
Kalıcı öneri yeniden okunurken tamamlanmış provider aşamasının süresine
kesinti süresi de eklendi: 10 saniyelik rezervasyona 24,837 saniye yazıldı
ve `episode_budget_overrun` kaydedildi. Bu sonuç başarılı resume sayılmaz.
Yeni süre tanınmadı, hata kaydı değiştirilmedi; aktif Scorer işi kalmadı
ve normal kuyruk işçisi yeniden açıldı. Tamamlanan provider süresinin
öneriyle birlikte kalıcı kaydı 0.36.2'de düzeltildi; ayrı 054 koşusu yukarıda
belirtilen kapsamda tamamlandı. 053 koşusunun hata ve stop kayıtları korunur.
[Kesinti ve hata kanıtı](review-evidence/resume053-interruption-outcome.json).

**Önceki ana sürüm 0.35.3:** PostgreSQL ve sentetik veri arayüzü, ayrıntılı
istatistik/JSON, LSH/OPTICS/SOM parametreleri ve UTC Scorer uyumu entegre.
Son kapı **session 19342 / exit 0**, **748 passed / 7 skipped / 82 deselected**;
yedi komut exit 0, strict mypy 122 kaynak, Pylint 9,29. İmaj eşliği ve 253
dosyanın ana ağaca byte eşitliği doğrulandı. Dar sentetik kayıt RPC'si gerçek
PostgreSQL'de 32/32, holdout kayıt işlevi düzeltmesi 6/6 testi geçti.
0028 ve 0029 ana DB'ye ayrı yedeklerden sonra uygulandı. Mod deneyleri için
holdout olmayan normal kapanış, mevcut sahiplik ve kalan süreyle sınırlıdır;
yeni bir süre veya değerlendirme yetkisi oluşturmaz. 11 odaklı kapanış testi geçti.
Tarayıcı veri seçimi, 18 özellik/12 ACF, indirme, kayıt ve parametreli başlatma
kontrolleri geçti. `716a8941-e913-4307-982d-f643e800a216` mod projesi
**12 gerçek Docker/Scorer ölçümü** (9 baseline + LSH/OPTICS/SOM için 3 aday),
6 geçerli deney/trajectory çifti ve **0 model token** ile tamamlandı.
API, DB ve kanonik rapor hash'i eşleşti. OMR, sensör katkıları, rapor indirme
ve masaüstü/mobil görünüm dahil **19 tarayıcı kontrolü** geçti. Önceki iki
grid hatası ayrıca saklandı. Bu kısa sentetik çalışma uzun ajan/public/AOS
kabulü değildir.
Asıl 22 M0 kabul sayısı değişmedi.
[Son kaynak ve gate kaydı](review-evidence/operating-console045-finalization-main-transplant.json),
[PG kayıt kontrolü](review-evidence/operating-console-045-pg-73b39639193f.json),
[PG holdout kontrolü](review-evidence/operating-console-045-pg-241786ae1a28.json),
[önceki tarayıcı ve grid hatası](review-evidence/operating-console045-browser-r4.json),
[tamamlanan proje kanıtı](review-evidence/operating-console045-completed-project.json),
[arayüzden deneme rehberi](43-first-project-guide.md).

**Önceki ana sürüm 0.36.0:** Aynı birleşik kaynakta gerçek izole PostgreSQL
R5 **33/33**, tam kalite kapısı **session 85033 / exit 0** ile **859 passed /
7 skipped / 98 deselected**; yedi komut başarılı, strict mypy 131 kaynak,
Pylint 9,26. 141 runtime dosyası için imaj eşliği ve 270 dosyanın gate → ana
ağaç byte eşliği doğrulandı. Ana DB yedeği alındı; 0030 migration exit 0.
Yalnız üç sahipli CPU servisi yenilendi ve sağlık/kimlik kontrolleri geçti.
Dağıtım sürücüsünün ilk anlık sağlık isteği API başlangıcına yetişmediği için
exit 1 döndü; sonraki ayrı kontrol sağlık, migration ve byte eşliğini exit 0
ile doğruladı. Bu başarısız deneme saklandı.

Gerçek eski `404d9a45-f1fb-4f91-a842-0edcd44bc1b9` stop kapanışı ise
**session 15531 / exit 1**: `baseline_calibrations` tablosu için yanlış DB
rolüyle okuma girişimi reddedildi. Bu tarihsel hata yukarıdaki 0.36.1
düzeltmesiyle giderildi. İlk gate ve tüm PG hataları da
saklanır. Daha sonraki gerçek süreç kesintisi/devam denemesinin başarısız
sonucu yukarıdaki 053 kaydındadır.
[Son gate](review-evidence/release046-gate-r2-execution-binding.json),
[PG R5](review-evidence/research046-pg-r5-execution-binding.json),
[ana sürüm dağıtımı](review-evidence/release036-main-deployment.json),
[ilk başarısız gate](review-evidence/release046-gate-r1-execution-binding.json).

**Gerçek public girdi hazırlığı:** Üretim yükleyicileri Genesis, GECCO,
CATSv2'nin üç sabit TSB dosyasını ve SMD `machine-1-1` için resmi eğitim/test
bölümlerinden 12.000'er satırı materyalize etti. Dört bağımsız aileye 0,25
üst sınırı uygulandı; Genesis NC ve SMD sunucu telemetrisi kimliği korundu.
Session **20531 / exit 0**, 3,861 saniye, yaklaşık 1 GiB cgroup tepe belleği,
swap 0; 33.255.744 bayt matris ve 42.215.235 bayt v3 manifest oluştu.
Bu hazırlıkta **36 baseline ölçümü planlı, ölçülen skor sıfır** idi.
Ardından **session 77401 / exit 0**, 13,785 saniyede dört güvenilir profil/etiket
kuruldu; Planner ile geri okuma ve yalnız baseline amaçlı registry yayını
geçti. API/arayıüz registry yenilemesinden sonra gerçek koşu
`53424e63-903a-4e48-83a7-7b89eaad0cca` **completed** oldu: **36/36 gerçek
Docker/Scorer ölçümü**, dört görev, üç algoritma × üç seed, 670,737 saniye,
kalibrasyon/bütçe doğrulanmış, bütün model kullanım sayaçları sıfır.
Observer session **81357 / exit 0**; strict rapor, API/DB ve kanonik SHA
`8cb2abd94ec92fad9079b69cb06bba44adb2a91867fd5699223c47afa15fd6ab`
eşleşti. Terminal tarayıcı session **83177 / exit 0**, izleme/rapor/indirme
akışının dört kontrolü geçti. İzlenen koşu türünün eksik görünmesi
0.36.1 ile düzeltildi. Bu seçili bölümler tüm veri setlerinin veya
public araştırma ajanının kabulü değildir.
[Tamamlanan public proje](review-evidence/public050-completed-baseline-proof.json),
[tam rapor](review-evidence/public050-completed-baseline-report.json),
[tarayıcı kanıtı](review-evidence/public050-browser-terminal-evidence.json).
[Girdi kanıtı](review-evidence/public050-materialization-proof.json),
[kurulum](review-evidence/public050-installed.json),
[yürütme](review-evidence/public050-install-execution.json).

**Önceki ana sürüm 0.34.0:** Baseline, Director nesil sahipliği, holdout
kapanışı, baseline arayüzü, LSH/OPTICS/SOM–NN–OMR ve temel istatistikler ana
koda aktarıldı. Tam kalite kapısı **session 64554 / exit 0**: yedi komut,
**708 passed / 7 skipped / 42 deselected**, strict mypy 113 kaynak ve
Pylint 9,30. İmaj eşliği geçti; doğrulanan ağacın 232 dosyası ana ağaçta
byte olarak aynı. Gerçek izole PostgreSQL R10 **session 76983 / exit 0**:
22/22 sahiplik/kapanış kontrolü geçti; süreç kurtarma seam'i bu testlerde
fixture olduğundan tam OS restart kabulü değildir. 90 sentetik sayısal
mod deneyi ve 12 istatistik testi ayrı kanıttır; yeni deney/istatistik
arayüzü ile gerçek model/AOS kabulleri açık kalır. Kabul sayıları değişmedi.
[Birleşik kaynak ve yürütme kaydı](review-evidence/combined034-main-transplant.json),
[PG R10](review-evidence/holdout034-pg-r10-execution-binding.json),
[ek kapsam](42-operating-modes-omr-experiments.md).

**Kullanılabilir bağlı CPU baseline:** `http://127.0.0.1:8788` arayüzü,
ayrı loopback API ve Director kuyruk servisi ana Lab PostgreSQL'e bağlı.
Gerçek tarayıcıdan başlatılan `26c9e91f-6db4-4733-bcb9-7ab698b2021b`
koşusu **36 gerçek Docker/Scorer ölçümü**, sıfır model çağrısı ve tamamlanmış
raporla sonuçlandı. Tarayıcı rapor kontrolü **session 50987 / exit 0**;
API raporu DB ile aynı, typed doğrulaması geçti ve tam JSON saklandı.
Mobil satır taşması ilk tarayıcı kontrolünde bulundu; 0.35.1 düzeltmesinin
gerçek mobil/masaüstü kontrolü geçti. Bu sentetik baseline akışıdır;
uzun agent/mod deneyi veya AOS/model kabulü değildir.
[Bağlantı ve tarayıcı kanıtı](review-evidence/console034-connected-bootstrap.json),
[tam rapor](review-evidence/console034-completed-baseline-report.json).

**Tarihsel özel 0.33 entegrasyonu:** Baseline + generation 1 sahiplik kaynakları ayrı
çalışma kopyasındaydı; o aşamada ana runtime 0.32.0 idi. Son özel imaj 113 runtime dosyasıyla
eşleşti; yedi komutluk kalite kapısı exit 0. R1 gerçek PG denemesi trigger alan
hatasını yakaladı; düzeltildi. R2 SQL owner alt kümesini geçti ancak test aracının
yetkisiz gözlem sorgusunda, production dispatch başlamadan exit 1 verdi.
Her iki başarısızlık ve R2'nin bağımsız, sahipliği doğrulanmış kaynak temizliği
korunuyor. R3’te 36 gerçek sentetik ölçüm artefaktı doğrulandı; kapanış
yolundaki ProgrammingError nedeniyle nihai rapor ve queued-stop kabulü yok.
Özel 0.33.1 sayısal sıfır düzeltmesi doğrudan PostgreSQL üzerinde 33 vakada
doğrulandı. 0.33.2 rapor sırası düzeltmesinden sonra **R6, session 56324 /
exit 0**: 36 benzersiz gerçek sentetik ölçüm, tamamlanmış nihai rapor ve
ayrı sıfır işli queued-stop doğrulandı; sahipli kaynaklar temizlendi.
Tam rapor JSON'u kalıcı saklanmadı; hash/matris kayıtları korundu.
Sonraki aktif-stop denemesinde çalışan Director-owned FIT sandbox'ın
API stop → `stopped` rapor → sahipli kaynak temizliği alt kontrolleri geçti;
tam canonical rapor saklandı. Ancak **session 6844 / exit 1**: dış envanter
kapısı `rclone-gdrive-sync.service` alt durumunun `start` → `auto-restart`
değişimini gördü. Genel test başarılı sayılmadı; otomatik tekrar yapılmadı.
Tam restart/holdout ve model/AOS kabulleri açık; kabul sayıları değişmedi.
`41-baseline-generation-integration-review.md`.

**0.32.0 kalibrasyon süreç/bütçe entegrasyonu:** 0022/0023, 135 hücrelik
sabit matris, geri ödenmeyen 100 saniyelik rezervasyon, ilk son tarih,
Scorer P=1 bekleme ve tam süreç nesli kontrolleri ana kodda. Astra kaynak
incelemeleri geçti. Gerçek PostgreSQL **session 70379 / exit 0**:
**1 test / 102,17 sn**, 135 sentetik bağlı receipt, freeze/yeniden okuma,
canlı kayıtta NULL nesil retleri, ilk son tarih ve rol/bütçe kontrolleri.
Gerçek hücre worker'ları ve Farm B kalibrasyonu çalıştırılmadı. Özel PG
kaynağı ayrı bağlıdır; ana metadata düzeltmesi PostgreSQL DDL'ini korur.
Son imaj **108 runtime dosyasında** eş. Tam kalite **session 69013 /
exit 0**, yedi komut: **539 passed / 7 skipped / 23 deselected**, strict
mypy 99 kaynak, Pylint 9,34. İlk SQL hatası ve iki başarısız gate korunur.
`38-care-calibration-execution-review.md` ve
`review-evidence/care-calibration-integration-032-quality-gate-binding.json`.
Baseline CLI ve bütün Director restart'ı özel ağaçlarda sürüyor;
kabul sayıları değişmedi.

**0.31.0 Thompson/Director entegrasyonu:** kalıcı v2 strateji durumu,
model öncesi seçim kaydı, tek terminal güncellemesi, failure bütçesi ve
v1/v2 rapor okuyucusu ana dala alındı. Astra kaynak incelemesi ve iki ek
regresyon sonrası hedefli **88 test** geçti. Yeni imaj **103 runtime
dosyasında** eş. Tam kalite **session 9344 / exit 0**, yedi komut başarılı:
**521 passed / 7 skipped / 22 deselected**, strict mypy 94 kaynak,
Pylint 9,37. Gerçek PostgreSQL v2 ve bütün süreç restart kabulü açık;
kalibrasyon 0022/0023 ve baseline CLI ürün kodu bu gate'e dahil değildir.
`36-thompson-loop-integration-review.md` ve
`review-evidence/thompson-integration-031-quality-gate-binding.json`.
Kabul sayıları değişmedi.

**Araştırma restart kapsamı:** Astra kaynak incelemesi güvenli yeni Director
nesliyle devam etmenin henüz uygulanmadığını doğruladı. Ham `lab run` yeniden
girişi desteklenen recovery yolu değildir; süreç sahipliği, atomik yazım
kontrolleri, kalıcı tekrar hakkı ve kesinti boyunca bütçe takibi eksiktir.
Mevcut checkpoint/seed geri okuma ve sentetik 20 öneri kanıtı bu gereksinimi
kapatmaz. [35-director-restart-scope-review.md](35-director-restart-scope-review.md).
Yeni drain gözlemci yardımcılarının 14 CPU testi geçti; gerçek SQL/süreç
sırası henüz kanıtlanmadı (`drain-before-cas-probe-030-cpu-review.json`).
Bu inceleme kabul sayılarını değiştirmez.

**0.30.0 holdout entegrasyonu:** bounded quota/reservation, bitless run-end
fence/rollback/replay, eski bütçe kayıtlarının korunması, Scorer recovery
ve Farm B yükleyici/adapter ana dala alındı. Astra/high kaynak incelemesi
geçti. Son gerçek PG wrapper kanıtı **session 51300 / exit 0**, 2 test ve
72 ek kontrol; sentetik ledger ile enjekte edilen hata sınırları ve marker
sonrası tekrar başlangıç çalıştı. Gerçek süreç ölümü/skorlama değildir.
Son imaj **103 runtime dosyasında** eş, tam kalite **session 70892 / exit 0**:
**487 passed / 7 skipped / 22 deselected**, strict mypy 94, yedi komut başarılı.
Gerçek worker/container drain, ölçülmüş Farm B kalibrasyonu/skorları,
10 KEEP/canary ve model/AOS birlikte çalışma açık. Ayrıntı
`32-holdout-integration-review.md`; bağ
`review-evidence/holdout-integration-030-quality-gate-binding.json`.
Kabul sayıları değişmedi.

**0.30 gerçek stop/recovery ve kalibrasyon saklama kanıtı:** Snapshot hazırlık
hataları düzeltildikten sonra **session 4504 / exit 0** ile çalışan holdout
worker'ı ve fit konteyneri yetkili API stop/production recovery yoluyla
kapatıldı. Bitless `failed` kaydı oluştu; kota ve skor sayıları değişmedi.
177 kaynak dosyası eş, özel DB ve erişim bilgileri temizlendi. Baseline ve
normalizasyon sentetik; API TestClient içindedir. Drain/CAS sırası bağımsız
olarak henüz ölçülmedi. Kalibrasyonun ayrı çalışma ağacında gerçek PostgreSQL
saklama/rol testi **session 20829 / exit 0**, **1 passed**: 135 sentetik
receipt, freeze, yarış, süre sonrası yeniden okuma ve erişim retleri geçti.
Kalibrasyon ana runtime'a alınmadı; kalıcı hücre sahipliği, süreç/bütçe
uygulaması ve 135 gerçek Farm B ölçümü açık. Ayrıntı
`34-lifecycle-and-calibration-review.md`; kanıtlar
`review-evidence/holdout-030-lifecycle-r10-execution-binding.json` ve
`review-evidence/care-calibration-031-pg-execution-binding.json`.

Bu review/test diliminin son tam kalite kapısı **session 80368 / exit 0**:
507 geçti, 7 atlandı, 22 live/GPU testi dışarıda; strict mypy 94 kaynak,
yedi komut başarılı. Ana runtime 0.30.0 ve önceki imaj kaynakları eş kalır.
`review-evidence/holdout-030-lifecycle-quality-gate-binding.json`.

**0.29.0 strateji/harness entegrasyonu:** pure Thompson politikası ve
önbellek/sandbox öncesi typed `harness_hash_mismatch` ret kontrolü alındı.
İmaj/tam kalite **session 92987 / exit 0**: **372 passed, 7 skipped,
13 deselected**, strict mypy 84, Pylint 9,40; yedi komut başarılı ve
93 runtime dosyasında imaj byte eşliği. Stratejinin Director bağlantısı,
tam CLI tamper/recovery ve gerçek PG ret kanıtı açık; M0.2 kapanmadı.
AOS V3 sürücüsündeki temizlik/ilerleme kanıtı sorunları kayıtlıdır ve V4
ayrı hazırlanıyor. Yeni GPU/model veya birlikte çalışma kabulü verilmedi.
Ayrıntı `29-strategy-and-harness-review.md`; kaynak/kanıt bağı
`parallel-integration-029-quality-gate-binding.json`. Kabul sayıları değişmedi.

**0.28.0 eğitim bakım entegrasyonu:** bounded TRAIN/noop ve 24k QLoRA
komutları, kalıcı bakım kayıtları, exact-generation stop, kira süresine
bağlı çocuk deadline/gate ve kaynak sınırları uygulandı. Gerçek CPU servis
başlangıcı/yol kontrolü geçti; gerçek GPU/eğitim henüz çalışmadı.
İmaj/tam kalite **session 69363 / exit 0**: **359 passed, 7 skipped,
13 deselected**, strict mypy 83, Pylint 9,40; yedi komut başarılı,
92 runtime dosyasında imaj byte eşliği. Kaynak/kanıt bağı
`parallel-integration-028-quality-gate-binding.json`; ayrıntı
`28-training-and-report-replay-review.md`.

**0.27 gerçek 20-deney rapor/replay kabulü:** üretim `lab run` 905,148 sn /
exit 0 ile 3 baseline + 20 öneriyi tamamladı: KEEP 1, KEEP_SIMPLER 1,
DISCARD 5, REJECT 13; 80 bağımsız Scorer sonucu. İlk inceleme sürücüsünün
baseline grafik beklentisi hatası saklandı; üretim koşusu tekrar edilmedi.
Düzeltilen, aynı kayıtları okuyan verifier **session 82460 / exit 0,
15/15 kontrol**: gerçek report/replay CLI, 21 grafik noktası/20 basamak,
22 karar kaydı (7 primary, 2 confirmed, 13 terminal), verdict/delta/ci/noise
bit eşliği. Ek C Referee 6/6 ve kümülatif sadeleşme fixture'ıyla
**M0.8, M0.11 ve M0.12 geçti**. Scoreless REJECT replay'ı immutable
neden kaydını doğrular; aday guard'ını yeniden çalıştırmaz. Bu sentetik
fixture kabulü public araştırma, gerçek Qwen veya AOS kabulü değildir.
Kanıt: `report-replay-twenty-027-v3-outcome.json`,
`referee-appendix-c-027-outcome.json`; ayrıntı
`28-training-and-report-replay-review.md`.


**0.27.0 entegrasyonu:** Lab CLI ve broker aynı sabit GPU kuyruk dosyasını
kullanabilir; dosya/dizin kimliği ve izin sınırları korunur. İmaj/tam
kalite **session 90787 / exit 0**: **334 passed, 7 skipped, 13 deselected**,
strict mypy 78, yedi komut başarılı. Gerçek model veya servis çalıştırılmadı.
`27-shared-gpu-deployment-path.md` ve kaynak bağı
`parallel-integration-027-quality-gate-binding.json` kapsamı kaydeder.

**0.26 güncel guard kabulü:** değişmeyen 19 saldırı/temiz aday kataloğu
gerçek üretim Docker yolunda **session 1913 / exit 0, 19/19 vaka ve 106 faz**
ile doğrulandı. M0.4, M0.5 ve M0.7'nin sentetik fixture koşulları kapandı;
public veri veya model kabulü verilmedi. Kaynak/imaj/temizlik bağı
`review-evidence/guard-docker-026-outcome.json`; ayrıntı
`26-broker-principal-followup.md`.
Ek C [3] üretim alarm fonksiyonları için 6/6 / exit 0 ve bit eşliği
sağlandı; bu kanıt ile aynı Docker testindeki dejenere/NRM-rampa retleri
M0.6'yı da kapattı (`alarm-appendix-c-026-outcome.json`).

**0.26.0 entegrasyonu:** ortak broker, canonical Lab araştırma üst
süreçlerini tam PID/başlangıç/boot/InvocationID/cgroup kimliğiyle yeniden
doğrular. Sabit owner çözümlemesi korunur. İmaj byte eşliği ve tam kalite
**session 96606 / exit 0**: **324 passed, 7 skipped, 13 deselected**,
strict mypy 78, Pylint 9.35; yedi komut başarılı. Bu CPU entegrasyonu yeni
bir model veya birlikte çalışma kabulü sağlamaz. Ortak DB yolunun CLI
uyumu, holdout ve eğitim kolları sürüyor. Ayrıntı
`26-broker-principal-followup.md`; kabul sayıları değişmedi.

**0.25.0 entegrasyonu:** CARE Farm A status maskesi resmî v6 kaynak
anlamına göre düzeltildi; yeni v3 session/split/suite ile 22 CARE görevinde
sağlıklı maruziyet ve PDM arıza desteği doğrulandı. 27 görevin gerçek ayrı
DB kayıt/Planner/API dosya kontrolü session 58545 / exit 0. Canonical Lab
principal kimliği gerçek systemd resolve+verify kontrolünü geçti. Qwen
çok satırlı kaynak şeması sabit xgrammar 0.2.7'de doğrulandı; trusted metin
sınırları korunuyor. Ek araştırma profilleri config digest'ine bağlıdır.
İmaj byte eşliği ve tam kalite **session 5452 / exit 0**: **314 passed,
7 skipped, 13 deselected**, strict mypy 78, Pylint 9.35. Gerçek S2 denemesi canlı AOS llama-server ortak kuyruğa katılmadan
başladığı için kesildi (**session 54140 / exit 1**, 100,747 sn); öneri
sonucu yok, yalnız sahipli Lab süreçleri boşaltıldı. 27 görevlik v3
sandbox/Scorer denemesi **27/27 / session 35503 / exit 0** ile geçti
(394,561 sn; robust-z seed 0, 12 PDM/10 NRM/5 EVT, tüm guard’lar). Tam
üç algoritma × üç seed kalibrasyonu ve araştırma hâlâ açık; kabul sayıları
değişmedi. Ayrıntı `25-public-scorer-and-runtime-followup.md`.

**Yeni gerçek public test bulgusu:** 27 görevlik v2 manifestin kayıt/okuma
kontrolü geçse de tam sandbox/Scorer kabulü geçmedi. Robust-z seed 0
**session 36524 / exit 1**, 1/27 tamamlandı; ikinci CARE görevi tamamen
maskeli olduğundan worker puan üretemedi. Salt okunur incelemede altı CARE
PDM görevinin tümü maskeli, 12 PDM'in 11'inde maskeden sonra pozitif örnek
yok. Bu v2 hatasının kaynak status/mask düzeltmesi v3 ile kaydedildi; gerçek
v3 Scorer tekrarı 27/27 geçti. Görevler skorla seçilerek atılmadı. `25-public-scorer-and-runtime-followup.md` ve ilgili gerçek
çalışma kanıtları geçerlidir. Kalite kapısı veri anlamı kabulü yerine geçmez.

**0.24.0 entegrasyonu:** 27 gerçek public görevin tam eval v2
manifesti trusted kayıt, ayrı Planner okuması ve API dosya doğrulamasını
geçti (**session 25825 / exit 0**): 72.329.043 bayt JSON, 52.126.816 bayt
matris, 971.333.632 bayt RSS. Önceki kısaltılmış iki EVT görünümünün
uygunsuzluğu ve tam kayıt bind-parametre hatası saklandı; görev seçimi
etiketlere göre ayarlanmadı. **Açık:** gerçek public Scorer/araştırma,
holdout ve daha geniş Qwen profili. EVT maske düzeltmesi gerçek worker'da
6/6; gerçek public zaman/maske kayıtlarında beş profil doğrulaması geçti.
27 görev kartı gerçek tokenizer'da 4259 token; 2048 token smoke profiline
sığmıyor (session 2120). Sınırlı onarım gerçek GPU'da çalıştı, fakat
iki çağrı da aynı geçersiz tek satırlık Python'u verdi (**session 30449 /
exit 1**, 11/13 yaşam döngüsü kontrolü; GPU 12860→62 MiB).
Geçerli S2 önerisi kabul edilmedi. AOS izole lifecycle/control yamaları
27 hedefli test/4100 paket kontrolüyle hazır; gerçek birlikte
araştırma/desktop açık. Son imajın 87 runtime dosyasında byte eşliği ve
yedi kalite komutu **session 23650 / exit 0**: **305 passed, 7 skipped,
13 deselected**, strict mypy 78, Pylint 9.35. Kaynak/kanıt bağı
`parallel-integration-024-quality-gate-binding.json` içindedir. Ayrıntı
`24-public-suite-and-bounded-repair-review.md`; kabul sayıları değişmedi.

**0.23.0 güncellemesi:** geçersiz Python AST'si mevcut tek onarım yoluna
alındı; aynı çıktı/süre/token sınırları korunuyor. İmaj byte eşliği ve yedi
kalite komutu **session 19551 / exit 0**: **292 passed, 7 skipped,
13 deselected**, strict mypy 77, Pylint 9.35. Kaynak/kanıt bağı
`parallel-integration-023-quality-gate-binding.json` içindedir.
Gerçek S2 provider bölümü **session 16408 / exit 1**: ilk model çağrısı
512 giriş/2837 çıktı token ile tamamlandı, onarım aktivasyon öncesi
`budget_exhausted` verdi. Geçerli öneri yok; 11/13 yaşam döngüsü/bütçe
kontrolü geçti, GPU 12854→62 MiB. AOS başlangıcındaki yetki değişimi
yarışı izole SQLite/fixture testinde yeniden üretildi; paralel düzeltme
ve normal bootstrap işi sürüyor. Kabul sayıları değişmedi; ayrıntı
`23-provider-syntax-and-aos-lifecycle-review.md`.

**0.22.0 entegrasyonu:** recovery, public-suite/PDM/NRM, HTML report ve tam
karar replay modülleri birleştirildi. Son imaj/byte parity ve tam kalite
kapısı **session 34679 / exit 0**: **290 passed, 7 skipped, 13 deselected**,
strict mypy 77 dosya, Pylint 9.35, yedi komut başarılı. Kaynak/imaj/kanıt bağı
`parallel-integration-022-quality-gate-binding.json` içindedir. İlk başarısız
entegrasyon kapıları korunur; ayrıntı `22-parallel-m0-integration-review.md`.

Gerçek izole PG/Docker/Scorer report/replay kontrolü **20/20 / exit 0**:
9 baseline + 6 proposal ölçümü, farklı seed skorlarıyla baseline/promoted
parent, dört karar bit eşliği, gerçek report/replay CLI exit 0 ve HTML hash
paritesi. M0.11/12 kısmiye ilerledi; bu sentetik EVT fixture'ı tam public
ve gerçek model araştırması değildir. CARE A'nın 22 görevi ve önceden
sabitlenmiş SMD makine/pencere materyalizasyonu exit 0 verdi. Tam süit,
holdout ve diğer gerçek kabul kapıları açıktır.

Ana DB migration zinciri veri/etiket/koşu satırları korunarak 0016→0018 ile
tamamlandı; alembic check exit 0. Tarihsel belgelerin yeni model varsayımları
yüzünden yanlış reddi düzeltildi; iki gerçek salt okunur CLI inspect 8/8.
Eksik tarihsel owner kanıtı nedeniyle bu koşular finalize edilmedi.

Kompakt 0.21 S2 tekrarı JSON parse'ını geçti fakat Python AST'sinde reddedildi
(session 27836 / exit 1). 0.22 sözdizimi tanısı dış GPU tüketicisi yüzünden
model sonucu üretmedi (session 44215 / exit 1). S2 araştırma ve AOS ile tam
birlikte çalışma kabulü verilmedi. Bu engeller CPU geliştirmesini durdurmuyor.

**Önceki 0.21.0 güncellemesi:** yerel hipotez/kaynak sınırları 384/6000 karakter;
istem v3 ve primary/repair şema/parser sınırları eş. İmaj byte eşliği
**session 76562 / exit 0**, tam kalite kapısı **session 50789 / exit 0**:
**242 test**, 11 deselected, strict mypy 69 kaynak, Pylint 9.32 ve yedi
komutun tamamı başarılı. Tek gerçek S2 denemesinde ortak kuyruğa katılmayan
dış Python GPU tüketicisi gözlendi; yalnız testin birimleri durduruldu.
**Session 20177 / exit 1**, nihai öneri yok; özel ticket active, son GPU
62 MiB. Bu kesinti kompakt istemi başarılı saydırmaz. Kanıt ve ana DB'nin
ayrı yedek/restore kontrolü `22-parallel-m0-integration-review.md` içinde.
Diğer paralel ürün dalları ve gerçek araştırma kabulü açık; 3/13/6 değişmedi.

**Son birlikte model tanısı:** 0.20 ürün kaynaklarında gerçek
Qwen S1 → AOS Decider → Qwen S2 → AOS Bonsai sırası başarılı:
**session 52269 / exit 0, 12/12 kontrol, 211,187 sn**. Dört ticket kapandı;
GPU zirve 12754 MiB, son 62 MiB; en düşük kullanılabilir RAM
19.787.816.960 bayt. S2 aritmetik JSON'u 140 çıktı token'ında tamamlandı.
`20-aos-broker-model-review.md` ve `aos-qwen-020-broker-model-outcome.json`
kapsamı kaydeder. Gerçek araştırma önerisi ve AOS desktop araçları bu
tanıda çalışmadı; aşağıdaki önceki başarısız tanılar tarihsel kanıttır.
Kabul sayıları değişmedi.

**0.20.0 güncellemesi:** kayıtlı S2 profilleri artık `top_k=20` kullanır; istek,
config ve receipt kimliklerine bağlıdır. Araştırma profili 4096 çıktı/512 düşünme
token'ı ve 210 saniye ile sınırlıdır. İlk gerçek deneme eski launch izin listesine
takıldı (session 63534 / exit 1, model yüklenmedi); liste kayıtlı profillerden
türetilerek düzeltildi. İkinci deneme **session 19049 / exit 1**, **196,756 sn**:
442 giriş/4096 çıkış token, nihai içerik 16893 karakter ve `finish_reason=length`.
**19/23 kontrol** geçti; ticket kapandı, tek model nesli/cgroup temizlendi,
GPU zirve 12832 MiB'dan 62 MiB'a döndü ve host rezervleri korundu.
Geçerli S2 CandidateProposal/tam araştırma kabulü hâlâ açık. Ayrıntı:
`22-parallel-m0-integration-review.md` ve `local-qwen-s2-profile-v2-after-review.json`.

Güncel tam kalite kapısı **session 1857 / exit 0**: **239 test**, 11 deselected,
strict mypy 69 kaynak, Pylint 9.32/10 ve yedi komutun tamamı başarılı.
Yeni imaj `cafb4f9c…` / **session 6186 / exit 0**, kaynak byte eşliği doğrulandı.
Kaynak/imaj/kalite ve başarısız S2 sonucu `local-qwen-s2-020-quality-gate-binding.json`
içinde bağlıdır. Paralel recovery/public/report/replay taslakları bu gate'e dahil değildir.

0.19.0 sonrası tek S2 CandidateProposal denemesi **session 34672 / exit 1**:
2048 çıktı/512 düşünme bütçesinde nihai JSON yok, `finish_reason=length`.
442 giriş/2048 çıkış token; ticket done, tek model generation ve temiz GPU/
cgroup doğrulandı. Önceki denemede review gözlemcisinin PID kapanış yarışı
vardı; düzeltmenin altı CPU kontrolü exit 0. Ayrıntı
`21-local-qwen-s2-research-review.md`; S2 ve altı öneri kabulü açık.
Kurtarma, report/replay ve public suite/PDM/NRM üç ayrı Luna/high ajanıyla,
ayrı kod kopyaları/veritabanlarında paralel ilerliyor. Kabul sayıları değişmedi.

Son gerçek birlikte çalışma ölçümü: üretim broker'ı ve ortak scheduler üzerinden
**Lab → AOS → Lab → AOS** sırası gözlendi. Qwen S1, AOS Decider ve Bonsai
başarılı; Qwen S2 tanı çağrısı 512 tokenlık çıktı sınırına takıldı.
**10/11 kontrol, session 74499 / exit 1**, 217,40 sn; dört ticket kapandı,
model süreçleri temizlendi ve GPU 62 MiB'a döndü (zirve 12756 MiB).
Bu, gerçek dönüşümlü model ilerlemesidir; tam araştırma/desktop kabulü değildir.
Önceki AOS-only test **8/8, session 38880 / exit 0** geçti; bu iki testin
süreleri ve kapsamları `20-aos-broker-model-review.md` içinde ayrıdır.

Önceki kod/kalite sınırı **harness 0.19.0**: **231 test**, strict mypy
**69 kaynak**, Pylint **9.32/10**, yedi komut **session 9552 / exit 0**.
İmaj `709275c8…`, 77 runtime dosyasında byte eşliğiyle doğrulandı
(session 10252 / exit 0). `aos-gpu-broker-019-quality-gate-binding.json`
son kaynakları, imajı, kalite kapısını ve birleşik denemenin üretim runtime
hash'lerini bağlar; S2 başarısızlığı korunur. Console entrypoint ve statik
systemd unit doğrulaması da exit 0; canlı AOS kurulumu yapılmadı.

Önceki 0.18 kapısı tarihsel kaynak kanıtıdır: 226 test/66 mypy kaynağı ve
`local-qwen-018-quality-gate-binding.json` içindeki gerçek PostgreSQL
**29/29** çağrı kurtarma, **30/30** bölümden devam, **51/51** dispatcher
kontrolleri (model/worker fixture). Son gerçek araştırma denemesi 0.17'deki
iki S1 önerisi/44 Scorer sonucu ve başarısız S2'dir; 0.18 denemesi baseline
sırasında dış GPU tüketicisi nedeniyle durdu: 23 skor, sıfır model çağrısı,
session 31486 / exit 1. Yeni birleşik tanı araştırma değildir.
Kabul sayıları değişmedi. Ayrıntı: `19-local-qwen-integration-review.md`.

Yerel sağlayıcının gerçek PostgreSQL checkpoint/oturum değişimi kontrolü **24/24, exit 0**; S1 ve S2→S1 token/receipt kayıtları korunuyor. Runtime ve son ledger insert test fixture'ıdır; gerçek model araştırması sayılmaz. Gerçek pinned tokenizer ölçümü, byte hesabının kabul ettiği geri bildirimli promptlarda **2564/2561 token > 2048** hatasını gösterdi (`local-qwen-tokenizer-size-review.json`); düzeltme kanıtı aşağıdadır. AOS'un **97 model/kod/runtime dosyası** salt okunur SHA kontrolünde manifestlerle eşleşti (`aos-local-model-pin-review.json`, exit 0); model veya canlı AOS başlatılmadı/değişmedi. Bu kontroller yeni bir tam kabul kapısı kapatmadı.

Tokenizer düzeltmesi üretim seçim yolu ve bağımsız gerçek sayımla **6/6, exit 0** doğrulandı: aynı geri bildirimli promptlar **1920/1917 token**; görev kartları/aday kodu korundu, en eski geri bildirimler çıkarıldı (`local-qwen-tokenizer-production-review.json`). CPU kontrolüdür; gerçek model araştırma ve AOS birlikte çalışma kabulleri açık kalır.

Güncel gerçek model denemesi: **gerçek S1 aritmetik doctor başarılı** (`native-qwen-s1-isolated-review.json`, session 30104 / exit 0 / 11 kontrol); tanı profili temperature=0/top_p=1. Başlangıç 78,838 sn, istek 0,621949 sn, drain 1,217 sn; GPU 62→12754→62 MiB. Model cgroup peak 10 GiB, gözlenen OOM/oom_kill 0. S2 düşünme bütçesi 128 tokena ayrıldı; 512 toplam token denemesinde nihai içerik oluştu ancak kesildiği için reddedildi (session 44665 / exit 1). Spec S2 örneklemesi (0,6/0,95) ile son denemede dış Python GPU tüketicisi ortaya çıktı; gözlemci yalnız kendi süreçlerini durdurdu ve sonuç oluşmadı (session 56456 / exit 1). **S2 henüz geçmedi.** M0.13 kısmi; araştırma S1 profili, altı gerçek deney/kapasite/egress, AOS birlikte çalışma ve eğitim açık. Ayrıntı ve başarısız/yarıda kesilmiş denemeler `18-native-runtime-review.md` içinde.

2026-09-25 native runtime ön kontrolü: özel user/network namespace ve Unix soketi **10/10**, aynı ağ alanında 1 MiB gerçek Torch CUDA işlemi **14/14**, iki komut da gerçek exit 0. GPU toplam bellek örnekleri 62→318→62 MiB; test process/cgroup temizliği doğrulandı. `IPAddressDeny=any` tek başına bu host'taki kendi loopback dinleyicimize erişimi engellemedi. Bu ölçümler model yükleme, üretim scheduler/lifecycle veya AOS birlikte çalışma kabulü değildir; kapı durumları değişmedi. Ayrıntı: `18-native-runtime-review.md`.

Native lifecycle güncellemesi: gerçek systemd CPU unit'leri ve fixture GPU gözlemcisiyle düzeltme sonrası **14/14 / exit 0**; normal kapatma, eski generation reddi, tekrar edilen GPU PID reddi ve yabancı süreç korunması doğrulandı. Gerçek modelin 16 dosyası/19,329 GB byte/hash kontrolünü 13,024 saniyede geçti; bu dosya kontrolünde model yüklenmedi. Foundation **6/6**, HTTP toplam deadline **3/3**, dar quarantine kayıt düzeltmesi **1/1**. Native 0.16.0 tam kalite kapısı **210 test / 65 mypy kaynağı / yedi komut exit 0** ile geçti; yeni imaj byte parity ve kaynak bağı `native-runtime-quality-gate-binding.json` içindedir. Önceki 177 testli gate 0.15.0 kanıtı olarak korunur. Gerçek Qwen araştırma, eğitim ve AOS birlikte çalışma kapıları açık kalır; ayrıntılı kaynak hash'leri `18-native-runtime-review.md` içindedir.

2026-09-25 AOS bileşen güncellemesi: gerçek typed AOS coordinator/policy/TrajectoryStore/DesktopController + ayrı AOS süreci + gerçek Lab HTTP/PG kontrolü **12/12**; tam typed araştırma→Director→Scorer→doğrulanmış rapor **22/22, 48 skor, exit 0**. Fixture desktop ve FixtureDecisionEngine açıkça kullanıldı. Güncel hedefli AOS testleri **32/32**, package gate **3842**, taze Lab tam gate **176 test/yedi komut** ve exit 0. M0.AOS.1 sentetik kapsamında geçti; gerçek outage/takeover ve model/GPU kapsamı açık. Ayrıntı: `16-aos-typed-policy-review.md`.

2026-09-25 gerçek araştırma ara gözlemi: çalışan `b54c9282-ca69-4deb-9e28-ef50e36bf21c` koşusunda **ilk gerçek S1 model önerisi dört görevde puanlandı**; üç baseline ile toplam 40 Scorer sonucu kaydedildi. Model receipt 860 giriş/1049 çıkış token, 78,309 sn başlangıç/21,455 sn çıkarım/0,521 sn drain bildiriyor. Tokenizer sayımı eşleşti, checkpoint fiziksel blob hash'i doğrulandı. `local-qwen-017-first-proposals-snapshot.json` bu ara anı kaydeder; altı öneri/S2/tam run verifier sonucu henüz yok, kabul sayıları değişmedi.

Sonuç güncellemesi: gerçek altı öneri denemesi **session 40641 / exit 1** ile S2'nin Markdown içine sardığı JSON yüzünden durdu. İki S1 önerisi ve üç baseline, **44 gerçek Scorer sonucu** üretti. Ayrı salt okunur teşhis **36/36, exit 0**: iki `DISCARD` kararı ham skorlardan verdict/delta/ci_low bit eşliğiyle doğrulandı (`local-qwen-017-failure-diagnosis.json`). Üç gerçek model çağrısı sonrası GPU/cgroup boşaldı, API yanıt verdi; kaynaklar sabit. S2 biçim onarımı yok ve Director çıktıktan sonra SQL `running` kalıyor; bu iki eksik giderilecek. Altı öneri/S2/tam run kabulü **geçmedi**; yukarıdaki ara gözlemler tarihsel, kabul sayıları 3/13/6 olarak kalır.

0.18 hazırlığında kurulu vLLM şema dönüşümü ve gerçek pinned tokenizer ile
CPU XGrammar kontrolü **7/7, session 55624 / exit 0** geçti
(`qwen-json-schema-cpu-review.json`). Geçerli CandidateProposal kabul edildi;
Markdown sarmalı, bilinmeyen move, ek verdict ve eksik kaynak reddedildi.
Bu Pydantic şemasıyla CPU uyum kanıtıdır; son üretim şeması, gerçek S2,
tek onarım ve kalıcı hata durumu henüz doğrulanmadı. Kabul sayıları değişmedi.

0.18 çağrı kesinti bileşeni: `local-qwen-attempt-recovery-review.json`,
**26/26, gerçek exit 0**. Gerçek PG checkpoint ve yeni Director oturumunda
tamamlanmış ilk yanıt yeniden kullanıldı; sonucu belirsiz ilk çağrı veya
onarım yeniden gönderilmedi. Ham yanıtlar ve önceki tüketim korundu.
Model/tokenizer ve son experiment insert fixture'dır; gerçek GPU/süreç
restart, loop'un sonraki ordinal'e ilerlemesi ve dispatcher terminal durumu
ayrıca açık kalır. Yeni tam kapı veya model kabulü verilmedi.
Ek kimlik/süre kontrolü **29/29 / exit 0**:
`local-qwen-attempt-recovery-identity-review.json`. Sağlayıcı/config ve
registry değişikliği reddedildi; yalnız bir saniye kalmışken tamamlanmış
yanıtın tüketimi korundu ve yeni model aktivasyonu yapılmadı.

0.18 başarısız bölümden devam bileşeni: `local-qwen-abandonment-after-review.json`,
**30/30, session 68567 / exit 0**. Gerçek PG ve üretim provider/bütçe/loop;
normal akış, bütçe kaydından sonra kesinti ve abandoned kaydından sonra kesinti
ikinci ordinal'e ulaştı. Model/tokenizer/kalibrasyon/süit kimliği fixture'dır.
İki çağrı ve 300 token bir kez sayıldı, şampiyon korundu, bozuk yanıttan deney
uydurulmadı. İlk `schema` alias hatası ayrı exit 1 kanıtında tutuldu.
Gerçek model ve genel dispatcher hata yaşam döngüsü açık; kabul sayıları değişmedi.

Sonraki dispatcher bileşen kontrolü **31/31 / exit 0**:
`director-failure-state-fence-review.json`. Gerçek PG/Director rolüyle claim
edilmiş temiz iş hatada failed; açık experiment varken gerçek SQL terminal
koruması korunarak stop_requested/kurtarma kaydı yazıldı. Concurrent stop ve
terminal durumlar, claim öncesi hata ve başka running durum korundu; sahte
rapor yok. Registry ve worker yürütmesi fixture; aktif süreç drain/kurtarma
ve önceki 0.17 koşusunun uzlaştırılması açık. Kabul sayıları değişmedi.


## 0.18 gerçek deneme — dış GPU tüketicisi nedeniyle durduruldu

Kaynak/imaj/tam kalite kapısı sonrasında `2c7f651` commit'iyle başlayan
`75642033-6e46-4a73-82a9-9e1ecb6066d3` koşusu **session 31486 / exit 1**
ile durdu. 330,428 saniyede ortak kuyruğa kayıtlı olmayan
`/home/cachyos/.venv/bin/python` süreci, `session-19.scope` içinde **3894 MiB**
GPU belleğiyle gözlendi. Kaynak uygulamanın AOS olduğu doğrulanmadı.

Bu anda baseline aşaması sürüyordu: **23 gerçek Scorer sonucu, sıfır yerel
model çağrısı, sıfır model önerisi**. Gözlemci yalnız kendi Director/API
süreçlerini ve sahipliği doğrulanan sandbox konteynerini temizledi; dış
sürece dokunulmadı. Dış süreç sonraki gözlemde kendiliğinden yoktu; son GPU
62 MiB. Kaynak/config ve runtime paketleri değişmedi.

Kanıtlar `local-qwen-018-wire-review.json` ve salt okunur SQL özetini taşıyan
`local-qwen-018-wire-outcome.json`. SQL koşusu `stop_requested`; bir baseline
`scored`, diğeri `primary_running`, rapor yok. Tam terminal kurtarma açık;
ledger yapay olarak completed/failed yapılmadı. Bu deneme **S2/altı öneri
veya AOS birlikte çalışma kabulü sağlamadı**. Kör tekrar yerine ortak GPU
entegrasyonuna devam edilir; kabul sayıları 3/13/6 kalır.

### Son 0.18 doğrulama sınırı

Yukarıdaki hazırlık kayıtları tarihseldir. Sabitlenen son kaynaklarda çağrı
kurtarma **29/29** (`local-qwen-attempt-recovery-018-final.json`) ve bölümden
devam **30/30** (`local-qwen-abandonment-018-final.json`), gerçek exit 0.
Dispatcher kontrolü **51/51 / exit 0** (`director-failure-state-return-review.json`)
ile normal dönen fakat terminalleştirilmemiş worker durumlarını da kapsar.
Bu koruma işçisiz `running` bırakmaz; olağan bütçe sonu raporu, gerçek PAUSED
yaşam döngüsü, aktif süreç drain/kurtarma ve eski 0.17 ledger uzlaştırması açık.
CPU'da derlenen şema son üretim şemasıyla aynıdır. **Gerçek S2/altı öneri kabulü
verilmedi; 3 geçti / 13 kısmi / 6 açık durumu değişmedi.**

Güncel AOS yama teslimi: **24 dosya**, bağımsız patch uygulaması ve tüm
kayıtlı kaynaklarda byte eşliği, **57 hedefli test / 9 tarayıcı testi atlandı**,
**4100 paket kontrolü**, exit 0 (`aos-lab-gpu-broker-patch-review.json`).
307 AOS Python dosyası gerçek birleşik tanı kaynaklarıyla eşleşti. İlk eski
manifest hatası ayrı before kaydında korunur. Yama kayıtlı baseline için
geçerlidir; canlı paralel değişiklikler aktarım öncesi yeniden uzlaştırılır.
Önceki 55 test/4096 kontrol/20 dosyalı rebase kanıtı tarihseldir.
Normal Lab bootstrap, yeni restart yolunun dış iş entegrasyonu ve tam
araştırma/desktop kabulü açık. Kabul sayıları 3/13/6 kalır.

## Spec §7.M0 — 15 kapı

| ID | Kabul | Durum / kanıt |
|---|---|---|
| M0.1 | Vendored VUS-PR/VUS-ROC ve upstream kaynaklı ≥30 golden fixture, \|Δ\|≤1e-9 | Geçti: 32 bağımsız golden fixture (random/good/constant/multi-event × pencereler 10–100), oracle NumPy 1.26.4, production NumPy 2.2.6; `pytest -q tests/test_vus_golden.py` → 37 passed/0, 6.85 s. Oracle provenance `review-evidence/tsb-ad-oracle/provenance.json`; golden SHA-256 `81c559a3449f83b3078fe93ec0b307cf460a2fd848c090c075153eba6791ad44`; input SHA-256 `d24cec2291711ac7758c6429a1875eb03a790f3b6f2f4b127f0702c635fcf1b5`. Vendored source exact SHA `1fcddedf5ada1d5221f39ee568c7fddb9e7181bd7e1c19636c2cbecf40c97707`; 32/32 değer tolerans içinde. |
| M0.2 | Trusted harness/evaluator/image/dependency byte değişikliği sonraki deneyde `REJECT(harness_hash_mismatch)` | Geçti: ana 0.37.0 ile byte eşliği doğrulanmış izole kaynakta, gerçek PostgreSQL ve üretim Director/Scorer/CLI yolları. Session 34680 / exit 0, 315,62 saniye: dokuz gerçek baseline skoru, dört ayrı tek-byte değişikliğinde sonraki öneri `REJECT(harness_hash_mismatch)`; yeni Docker admission, Scorer işi veya skor yok. Dört tam CLI resume çağrısı exit 1; özgün deadline/bütçe/control/owner/nesil ve dokuz skor korundu. Yedi strict belge çifti; kaynaklar geri yüklendi, yalnız sahipli test süreçleri/DB/credentials temizlendi. Bu başarılı restart veya gerçek model kabulü değildir. [İcra bağı](review-evidence/tamper057-execution-binding.json), [PG sonucu](review-evidence/tamper057-tamper-057-pg-768bf8bc14aa.json), [CLI sonuçları](review-evidence/tamper057-cli-result.json). Arayüz kanıtları: `review-evidence/tamper057-execution-binding.json`, `review-evidence/tamper057-summary.json`, `review-evidence/tamper057-cli-result.json`. |
| M0.3 | İzolasyon: DNS/HTTP reddi, veri/etiket dosyası yok, fit eval görmez, GPU yok, supervisor PID/mem erişimi yok, prompt injection araç yüzeyini aşamaz | Kısmi: güncel 0.37.0 source/image bağıyla observer R5 numeric/thread/signal/forged-exit testleri ve 058 R2 beş gerçek test geçti (session 34318 / exit 0). DNS resolver, UDP53, HTTP girişimleri hata swallowed olsa da exit68 ret; typed fit train-only, ayrı score, host label/eval dosyası/attrs/private index yok; gerçek tool_calls sandbox çıktısı trusted Scorer parser ret, host canary korundu. Altı konteynerin network-none/nonroot/noGPU/RO/mount/cap/PID/CPU/RAM ayarları ve temizliği doğrulandı. Eski19 guard/current crash-window ve supervisor memory erişimi için önceki source-bound kanıtların güncel kaynağa recertification kapsamı hâlâ sınırlı; gerçek model prompt robustness ölçülmedi. `review-evidence/sandbox058-summary.json`, `review-evidence/sandbox058-r2-receipt.json`, `review-evidence/observer0370-probe-r5-strict-supplement.json`. R1 test aracı fit_artifact eksikliğiyle exit1 sonucu korundu. |
| M0.4 | Causal ve leaky/centered/stateful negatif fixture'lar | Geçti: güncel 0.26 kaynak/imajında gerçek Docker kontrolü session 1913 / exit 0, 19/19 vaka ve 106 faz. Temiz causal ve stateful adayda ihlal 0.0; eval istatistiği (normal/küçük ölçek) ve centered rolling adayları causality ile reddedildi. Stateful skorun dört causal çağrısı aynı frozen fit hash'ini ayrı konteynerlerde açtı. `review-evidence/guard-docker-026-outcome.json`; kaynaklar sabit, kendi konteynerleri temiz. Bu sentetik fixture kabulüdür; public araştırma/LLM/AOS kabulü değildir. |
| M0.5 | Sabit seed'de full fit+score determinism ≤1e-7, tohumsuz RNG reject | Geçti: 0.26 gerçek Docker fixture'ında aynı ctx.seed ile bağımsız tam fit+score tekrarlarının bağıl farkı 0.0; fit-only ve score tohumsuz RNG adayları determinism ile reddedildi. Session 1913 / exit 0; `review-evidence/guard-docker-026-review.json` her fazın girdi/kaynak/artefakt hash'ini kaydeder. Tam araştırma ve public veri kabulleri ayrıdır. |
| M0.6 | Sabit/NaN/uzunluk reject; NRM-only position bias; always-on PDM/NRM 0; Ek C parity | Geçti: session 1913 / exit 0 gerçek Docker kataloğunda sabit/NaN/Inf/yanlış uzunluk adayları reddedildi; NRM zaman rampası position_bias verdi. Ayrı mevcut-sürüm CPU kontrolünde Ek C [3] dwell/release, üç PDM skor/FA/doluluk üçlüsü ve NRM örneği float.hex düzeyinde eşleşti; hep açık PDM/NRM alarmı ret üretmeden 0 puan aldı (6/6, gerçek exit 0). `review-evidence/guard-docker-026-outcome.json` ve `review-evidence/alarm-appendix-c-026-outcome.json`. Bu tanımlı fixture kabulüdür; tam public araştırma ayrıca açıktır. |
| M0.7 | Görev ID/zaman aralığı/64 sayı hardcoding reject, temiz aday pass | Geçti: 0.26 üretim guard yolunda literal task ID, UTC eşdeğer eval zamanı ve pozitif/negatif 64-float listeler doğru hardcoding koduyla reddedildi; dört temiz aday geçti. Session 1913 / exit 0; `review-evidence/guard-docker-026-outcome.json`. Bu sabit saldırı fixture'larının kabulüdür; genel ezber tespiti veya public benchmark kontaminasyonu garantisi değildir. |
| M0.8 | Referee referans ve iki seed/noise, KEEP_SIMPLER sınırı; ledger skorları | Geçti: Ek C Referee 6/6 (gerçek exit 0; review-required null REJECT), üç tohumlu gerçek baseline/noise, KEEP ve KEEP_SIMPLER için iki karar tohumu ile üçüncü noise ölçümü ve sabit historical-best çizgisi doğrulandı. 20 önerilik gerçek Docker/ayrı Scorer koşusunda 36 baseline/80 toplam skor, 7 ölçülmüş kararın delta/ci/noise bit eşliği ve parent lineage mevcut. Kümülatif sadeleşme sınırı üç fixture ile equality/next-lower-float dahil geçti. `referee-appendix-c-027-outcome.json`, `referee-cumulative-floor-review.json`, `report-replay-twenty-027-v3-outcome.json` (15/15; session 82460 / exit 0). Bu dev fixture kabulüdür; public/model araştırması ve bağımsız genelleme açık kalır. |
| M0.9 | Fake LLM ile 20 deney; tüm verdict/crash türleri; sha256 blob; timeout reject | Geçti: `.venv/bin/python docs/ai-scientist/review-evidence/review_director20.py` → exit 0, 8/8 kontrol. Üretim `lab run`, dört sentetik EVT görevinde üç baseline + 20 proposal'ı müdahalesiz tamamladı: KEEP 1, KEEP_SIMPLER 1, DISCARD 5, REJECT 13 (iki aday çökmesi ve gerçek uzun-fit timeout dahil). 36 baseline + 44 aday/guard öncesi görev ölçümü = 80 ayrı Scorer skoru; 23 experiment/trajectory belge çifti ve fiziksel hash-doğrulamalı bloblar; sealed plan sonrası ayrı finalizer ile 80 skorlu rapor. Yedi ölçülmüş karar ham Scorer skorlarından yeniden kuruldu; verdict/delta/ci_low ve önceki noise bit eşliği geçti. `review-evidence/director20-cli-review.json`; harness `8f222823…`, imaj `c33fb006…`, kaynaklar sabit. Gerçek model/public veri/GPU/AOS, süreç restart/stop, Git ref soy ağacı ve ürün report/replay komutları bu sentetik kabulün dışında açık kalır. |
| M0.10 | Her 10 KEEP ve koşu sonunda holdout, yalnız bit; kanarya sızıntısı yok | Açık: 0.32 ana kodda kalibrasyon 0022/0023, sabit 135 hücre matrisi, ilk deadline, geri ödenmeyen rezervasyon ve tam süreç nesli recovery kontrolleri var. Gerçek PostgreSQL session 70379 / exit 0 ile 1 test geçti: 135 sentetik receipt, freeze/readback, canlı kayıtta NULL generation retleri, değişmeyen bütçe ve ilk hücre deadline sonrası replay. 178 özel kaynak hash’i eş; kendi DB/erişim bilgileri temiz. Ana metadata farkı PostgreSQL DDL’ini değiştirmiyor. Bu gerçek 135 worker/ölçüm değildir. Önceki gerçek Farm B kurulum/Scorer okuması 15 görev/87.248 noktada geçti, normalization sentetik. Ana 0.30 wrapper testi session 51300 / exit 0 ile 2 test +72 kontrol geçti; marker sonrası startup tekrarında yeni iş/kota/sonuç yok. Önceki stop/recovery session 4504 / exit 0 çalışan holdout worker/fit konteynerini durdurdu; bitless failure/kota korunumu gözlendi. Bağımsız drain/CAS sırası, gerçek kalibrasyon ve Farm B skorlama, 10 KEEP/run-end/canary açık. `38-care-calibration-execution-review.md`, `review-evidence/care-calibration-031-pg-r3-execution-binding.json`, `34-lifecycle-and-calibration-review.md`, `review-evidence/holdout-030-lifecycle-r10-execution-binding.json`, `32-holdout-integration-review.md`. Gerçek veri kalibrasyonunun tarihli sayacı: `review-evidence/public056-calibration-progress.json`; ilk pre-sandbox hatalı attempt korunur. Onarılan kaynakla ayrı attempt eski deadline içinde 135/135 gerçek ölçümü tamamladı; son suite registration aşamasında launcher exit1 verdi. Sonraki ayrı registrar recovery session 26008 / exit 0 ile iki suite kaydını bağladı; iki attempt'in tüm ölçüm/süre/bütçe satırları değişmedi. Özgün kaynakla bağımsız proof session 58086 / exit 0 ile 135 hücreyi, 15 özeti ve sabit deadline/rezervasyonu doğruladı. Özgün kalibrasyon launcher exit 1 kaydı korunur. Sonraki gerçek public araştırma 18/243 baseline ölçümünden sonra `stop_requested` oldu; wrapper/driver exit 1, canonical owner exit 0 fakat `recovery_required`. Aday/holdout aşamasına ulaşmadı. Süre ve ölçümlü-baseline kapanış düzeltmesi ayrı kopyada sürüyor; araştırma/gerçek holdout kabulü açık. `61-baseline-duration-and-stop-recovery.md`, `review-evidence/public056-research-dispatch-failure-summary.json`. `46-public-measured-calibration.md`, `review-evidence/public056-measured135-readonly.json`, `review-evidence/public056-calibrate-repair-execution.json`, `review-evidence/public056-registration-readonly-diagnosis.json`. |
| M0.11 | `lab report` HTML merdiven/ledger parity | Geçti: gerçek `lab report` CLI exit 0; 20 öneri/3 baseline sayıları ve KEEP 1/KEEP_SIMPLER 1/DISCARD 5/REJECT 13 dağılımı immutable ledger ile aynı. HTML hash, 21 gerçek sequence/score noktası ve 20 H/V basamağı eşleşti. `report-replay-twenty-027-v3.html` ve `report-replay-twenty-027-v3-outcome.json` (session 82460 / exit 0, 15/15). Önceki verifier hatası ve özgün 905 sn üretim koşusu korunur. Public/gerçek model raporu ilgili araştırma kapılarında açık. |
| M0.12 | Replay kararı verdict/delta/ci_low bit parity | Geçti: gerçek `lab replay` CLI, aynı 20 önerinin 22 kaydını fiziksel hash-doğrulamalı bloblardan doğruladı: 7 primary + 2 confirmed karar yeniden hesaplandı; 13 scoreless terminal REJECT immutable neden kaydından kontrol edildi. Verdict ve sonlu delta/ci_low/noise float.hex bit eşliği, null alanlar ve tüm kayıtların kapsanması geçti. Scoreless retlerde guard yeniden çalıştırıldığı iddia edilmez. `report-replay-twenty-027-v3-outcome.json` (session 82460 / exit 0, 15/15); önceki eksik/değişmiş blob negatifleri `report-replay-production-pg-cli-022.json`. Public/model replay kabulü ayrı araştırma kapsamındadır. |
| M0.13 | Qwen3.5-9B gerçek yerel S1/S2, doctor throughput/capacity/egress raporu | Kısmi: 2026-10-02 altı önerili gerçek yerel çalışma modu araştırması completed; S1×2/S2×4, LSH/SOM/OPTICS, 15 Scorer ölçümü, 0 parse/repair/fallback. 934,062 s; 8278 token; 12890 MiB tepe VRAM. Altı DISCARD; sentetik tek snapshot üzerinde iyileşme yok. Ayrı daldaki explicit artifact_root replay düzeltmesiyle altı karar birebir doğrulandı; GPU ve geçici işçi/DB kapanışı geçti. Altı öneri/S1–S2/parse alt eşiği karşılandı; 32k×2 kapasite, tam host egress ve gerçek AOS birlikte çalışma hâlâ açık. `118-six-proposal-local-research.md`, `review-evidence/mode-six-research-20261002.html`, `review-evidence/mode-six-research-20261002.json`; önceki kanıtlar `19-local-qwen-integration-review.md`, `22-parallel-m0-integration-review.md`, `06-aos-coordination.md`. |
| M0.14 | Ortak GPU lease SERVE↔TRAIN noop ve 24k QLoRA VRAM ölçümü | Açık: gerçek TRAIN/noop→SERVE ve 24k/3 adım QLoRA ölçümü yapılmadı. 0.28 komutları, immutable bakım ledger'ı, exact child generation/gate/drain, kira deadline/heartbeat ve kaynak limitleri entegre. Gerçek CPU parent-context probe ve 24 odaklı test geçti; ana tam gate 359 test/exit 0. `training-maintenance/delivery.json`, `training-runtime-corrections-delivery.json`, `parallel-integration-028-quality-gate-binding.json`; ADR 0014/0015. Pinned Unsloth ortamı metadata ile doğrulanmıştır; bu sonuç kapasite veya gerçek model kabulü değildir. Canlı AOS GPU koordinasyonu beklenir. |
| M0.15 | Kalite kapısı, CHANGELOG, PR base main, merge yok | Kısmi: ana runtime 0.41.0; tam gate 1247 passed / 7 skipped / 120 deselected, yedi komut exit 0. Dağıtılan 306 runtime dosyası geçen gate ile byte eş; offline imaj eşliği doğrulandı. Gerçek ana DB clone yükseltme/geri alma/yeniden yükseltme ve gerçek tek Scorer işi geçti. Session 86085 / exit 0 ile 13 payload ve 0034/0035 dağıtıldı; 47 tablo / 234.073 satır, bütçe/deadline/generation, rol ve ACL korundu. Üç sahipli CPU servisi aktif, tünel aynı. [Gate/imaj](review-evidence/release041-image-gate-r4-summary.json), [dağıtım](review-evidence/release041-deployment-summary.json), [Scorer](review-evidence/release041-worker-process-r3-summary.json). Önceki başarısızlıklar korundu. Remote/auth yok, PR açılmadı; merge yok. |

## Review eki — ek 7 kapı

| ID | Kabul | Durum / kanıt |
|---|---|---|
| M0.AOS.1 | Gerçek AOS typed task/tool/policy ile sentetik suite start→poll→verified report, kimlik/hash eşliği | Geçti: `review_aos_typed_wire.py --execute` gerçek ayrı AOS process/TrajectoryStore/DesktopController/typed State-Action/ToolRegistry/SafetyPolicy ve Lab HTTP→Director→Docker→bağımsız Scorer yolunda **22/22, exit 0** (`aos-typed-director-review.json`). Açık FixtureDecisionEngine, fixture desktop sınırı ve dört sentetik EVT görev kullanıldı. Üç baseline + bir KEEP adayı 48 skor, dört hash-doğrulamalı belge çifti ve kapalı plan üretti; kimlik/bütçe eşliği ve ham skor karar bit eşliği geçti. AOS 37 typed action ve matching verification sonrası SUCCEEDED oldu; report SHA `f1af9ac3…`, training_eligible=0. 302 Lab kontrol örneği/max status 2,13 ms, 32 typed status örneği; kaynak ve harness sabit. Gerçek model/GPU, production bootstrap, public süit ve tam restart/stop diğer açık kapılardadır. |
| M0.AOS.2 | Aynı key/payload idempotent; key/payload uyuşmazlığı, yanlış owner/lease/tool reddi | Kısmi: 2026-09-30 gerçek ayrı süreç/SQLite/HTTP koşusunda eşzamanlı aynı-key start tek durable job, bekleme sonrası eski lease ve yanlış tool retleri geçti. R2 barrier timeout exit 1 korunur; [güncel kapsam](review-evidence/aos042-cpu-lifecycle-summary.json). Dar güncel AOS yaması izole teslimdedir; canlı kurulum ve tam gate kapanışı yok. Önceki kanıt: gerçek loopback Lab sunucusu + ayrı AOS ortamındaki gerçek LabApiClient + PostgreSQL kontrol koşusu 21/21 ve exit 0 (`review-evidence/api-control-wire-review.json`). Aynı istek aynı run, değişen payload 409, aynı external action için yeni key 409; başka owner status/stop/report 404; eksik AOS kimliği ve geçersiz süit/bütçe 422. Bu taşıma/DB kontrolüdür; gerçek AOS tool/policy, lease ve takeover kabulü açık. Yeni delayed-decision fixture incelemesi karar beklerken değişen lease sonrası start açığını doğruladı (`aos-lab-start-await-race-before.json`, exit 1); ayrı kopyada düzeltme sürüyor. |
| M0.AOS.3 | Stop/takeover/restart idempotency; yeni yetki olmadan restart yok; bağımsız doğrulama | Kısmi: 2026-09-30 gerçek child restart/SQLite reopen ve TCP console outage/rebind geçti; aynı run R3 devamı 10/10 exit 0 ile yeni yetkili recover, aynı-binding stop tekrarı ve foreground ilerlemesini ölçtü. Ayrı normal recovery exit 0 ve API/hash readback ile stopped raporu doğrulandı. Otomatik tek stop terminalizasyonu ve Scorer inflight drain açık; [güncel kapsam](review-evidence/aos042-cpu-lifecycle-summary.json). Önceki kanıt: tarihsel `Unknown run` açığı typed runtime state ile giderildi. Güncel hedefli AOS gate 32/32 içinde gerçek TrajectoryStore reopen/reconcile, açık yeni HUMAN lease ile rebind, aynı stop tekrarı, eski yetkiyle kayıp start yanıtını yeniden göndermeme ve Lab stop hatasında console kontrolünün sürmesi sınanır (`aos-typed-package-gate.json`). Taşıma/controller fixture'ları ve gerçek SQLite/ASGI route kullanılır; tam süreç/HTTP restart veya in-flight drain kabulü değildir. Takeover sorgusunun dokuz iş sınırı, admission tavanı, bilinmeyen Lab handle uzlaştırması ve gerçek koşudaki stop/drain açık. |
| M0.AOS.4 | İki process GPU ownership yarış, timeout/crash/stale/recovery | Kısmi: 0.15.0 üretim scheduler iki sabit principal için kalıcı dönüşümlü sıra, MainPID/InvocationID/cgroup/PID-start/boot kimliği, immutable istek bütçeleri ve fencing uygular. Süre aşımı ve eski boot karantinaya alınır; trusted drain olmadan devir yok. `gpu-systemd-race-review.json` gerçek iki systemd CPU servisiyle 11/11, exit 0: 20 dönüşümlü turn, 10+10 ilerleme, dış principal reddi, her canlı CPU alt sürecinde bırakma reddi ve önceki alt süreç bitmeden sonraki lease verilmemesi. `gpu-phase-bound-review.json` 7/7 süre kontrolü; bitmiş ready tekrarı ve değiştirilmiş receipt bütçesi düzeltildi. Kuyruk testleri 11/11. Bu tarihsel CPU gözlemcisi CUDA kanıtı değildir. Sonraki izole AOS hook/UDS/broker denemesinde gerçek Decider ve Bonsai recovery 8/8 / exit 0 ile tamamlandı; iki model süreci ve GPU belleği boşaldı (`20-aos-broker-model-review.md`). Sonraki üretim broker/Qwen birleşik tanıda gerçek Lab→AOS→Lab→AOS sırası, dört kapalı ticket ve drain gözlendi; S1/Decider/Bonsai geçti, S2 token sınırına takıldı (10/11, session 74499 / exit 1). Bu kısa tanı iki sahip ilerlemesini gösterir; tam crash/recovery ve araştırma/desktop donanım kabulü açık. Canlı AOS değişmedi. |
| M0.AOS.5 | Gerçek yerel AOS kararı ve Qwen S1/S2 ile kısa koşu | Açık: 0.20 birleşik gerçek model tanısında Qwen S1/Decider/S2/Bonsai dört çağrı başarılı ve dönüşümlü (session 52269 / exit 0, 12/12). Aritmetik/sentetik girdiler kullanıldı; AOS modeliyle başlatılan ve Qwen S1/S2'nin araştırma yaptığı başarılı birleşik koşu henüz yok. |
| M0.AOS.6 | AOS hedefli test ve package validation + Lab gate; bağımsız/AOS başlatma docs | Kısmi: güncel iki dosyalık ek teslim için manifest, temiz kopyada check/apply/byte eşliği ve 4360 package kontrolü gerçek exit 0; [R4 teslim](review-evidence/aos042-package-r4-summary.json), [iki başlatma yolu](63-aos-opt-in-start.md). Güncel Lab kapısı 1247 test/yedi komut exit 0. Normal Decider/shared-GPU bootstrap ve canlı AOS kurulumu çalıştırılmadı. Önceki kanıt: güncel canlı AOS kaynak kopyasına bağlı 31 dosyalık izole rebase, session 37161 / exit 0 ile 109 hedefli test (1 pinned Chromium testi atlandı) ve 4360 package kontrolünden geçti. Check/apply ve ayrı temiz kopyada tam byte eşliği doğrulandı; canlı AOS değiştirilmedi. `review-evidence/aos-current-rebase-20260929-receipt.json`, `06-aos-coordination.md`. Önceki 24 dosyalık AOS broker yamasında 57 hedefli test / 9 tarayıcı skip ve 4100 package kontrolü exit 0; tüm patch byte eşliği `aos-lab-gpu-broker-patch-review.json` içinde. Önceki source-typed üzerinde 32 hedefli test ve 3842 package kontrolü exit 0; tam çıktı/kaynak hash'leri `aos-typed-package-gate.json`. Typed AOS diliminin Lab 0.14.0 tam gate exit 0, 176 test/63 mypy kaynağı/yedi komut (`aos-typed-lab-quality-gate-binding.json`). Güncel Lab genel kalite kapısı M0.15 kaydındadır. On bir dosyalık typed patch, kayıtlı source-current test kopyasına uygulanıp byte eşliğiyle doğrulandı; eski mapping patch ile manifest farkı nedeniyle kör zincirleme yapılmaz. Normal AOS bootstrap'ında opt-in yapılandırma ve kullanıcıya yönelik iki başlatma yolunun tam belgesi açık. Tam AOS pytest başarılı sayılmadı; canlı AOS değişmedi. |
| M0.AOS.7 | AOS interaktif görev + Lab research aynı anda; gecikme/GPU/RAM/CPU/switch/queue; iki iş verified, OOM/starvation yok | Açık: 0.20 kısa gerçek tanısında dört model çağrısı dönüşümlü tamamlandı; host RAM/disk/sıcaklık rezervleri ve drain doğrulandı (12/12, session 52269 / exit 0). Tam interaktif AOS görevi + Lab araştırması, toplam CPU/host yükü ve gecikme/OOM/starvation kabulü henüz yok. V4 sürücüsünün bootstrap/cleanup ve aynı aktif aralıkta ilerleme denetimleri 11 CPU sözleşme testiyle doğrulandı; gerçek çalışma yapılmadı. `review-evidence/aos-lab-coexistence-v4-cpu-review.json`, `32-holdout-integration-review.md`. |

## Host / paylaşım kapıları

### 2026-09-27 kullanıcı kapsamı: çalışma modları ve deney laboratuvarı

[Çalışma modları, OMR ve deney gereksinimleri](42-operating-modes-omr-experiments.md)
LSH/OPTICS/SOM → mod toleransları → nearest neighbor → sürekli OMR akışını,
TrendMiner SOM tanılarını, temel istatistikleri, veri tabanı seçimini ve geniş
sentetik test havuzunu tanımlar. OM.1–5 ve OM.7 kısmi, OM.6 açıktır: 90 gerçek
sayısal CPU koşulu ve 12 istatistik testi geçti; arayüz/Director/Scorer ve uzun
agent kabulünün yerini tutmaz. Önceki 22 M0 maddesinin
10 geçti / 8 kısmi / 4 açık özeti bu kapsam eklenince genel ürün yüzdesi olarak
kullanılamaz; yeni kapsam ayrıca kanıtlanır.

P=1, sandbox ≤4 GiB ve ≤2 CPU; AOS/host rezervleri; servis/DB/dependency/port/cache/output/lock izolasyonu; ≥20 GiB boş disk; GPU doluyken health/status/stop yanıtı ve CPU araştırma; kısa adil model dilimleri ve dönüşümlü hizmet sırası. Bunlar M0.AOS.7 ile ölçülmeden kabul edilmez. Başlangıç keşif: `review-evidence/host-inspection.json`; `nvidia-smi` 16,376 MiB, Docker 29.8.1, boş disk ~103 GiB. 11434 ve diğer mevcut listener'lara dokunulmaz.

## Kanıt günlüğü

- 2026-09-30 AOS kontrol kaynak teslimi: ayrı authenticated
  capability/status/cancel/reconcile, aynı tek arbiter DB'sinde kalıcı iptal ve
  terminal receipt, bounded restart uzlaştırması ve unbound child için
  fail-closed quarantine eklendi. İzole CPU kontrolü 95 passed; public genel
  gate 1479 passed / 7 opt-in skipped / 120 GPU-live deselected, yedi komut
  exit 0. [Kaynak teslimi](77-aos-control-source-delivery.md). AOS ortak wire/
  endpoint/capability teyidi ve gerçek GPU kabulü açık; toplam değişmedi.

- 2026-09-30 R10: 36 CPU baseline, G1/G2 fencing ve gerçek W1 committed CAS
  gözlendi. Özel gözlemcideki `timedelta` scope hatası nedeniyle parent exit 1;
  planlanan W1 crash, W2 retry ve drained ledger retry çalışmadı. Bağımsız
  fiziksel karantina kontrolü exit 0: 37 Scorer + sahipler/recovery/API/parent
  kapalı, aktif job 0; expired pencere, `stop_requested`/`pending`/rapor NULL ve
  snapshot değişmeden korunuyor. Terminal/GPU kabulü yok; toplam artırılmadı.
  [Kanıt ve sınırlar](76-native-r10-quarantine-proof.md).

- Plan başlangıç incelemesi: `docs/ai-scientist/03-architecture-review.md`, `04-m0-review-addendum.md`, `review-evidence/review-results.json`, `review-evidence/host-inspection.json`.
- İlk typed contract/Referee dilimi: `harness/contracts.py`, `harness/referee.py`, `harness/guards.py`, `lab/llm/router.py`; olumsuz/replay testleri `tests/test_referee.py`.
- İlk tam gate: `uv run --python 3.12 --extra dev python scripts/quality_gate.py`; Ruff 0, Pylint 0 (9.95/10), Bandit 0, pytest 14 passed/0, mypy --strict 0. Çıktı pipe edilmedi; `scripts/quality_gate.py` her alt komutun exit code'unu yazdırır. GPU/live testleri dışarıda kaldı.
- İlk gate denemesi fail oldu (Ruff evidence source/style, mypy replay untyped coercions); ikisi kapatıldıktan sonra gate yukarıdaki gerçek exit code'larla yeniden geçti.
- M0.1 dilimi gate: `evidence/quality-gate-latest.json` içinde her komutun tam stdout/stderr/exit code'u ve build wheel import smoke'u var; `overall_exit_code=0`.
- M0.6 metric dilimi gate: `evidence/quality-gate-latest.json`; Ruff/Pylint/Bandit/mypy/wheel smoke `exit_code=0`, pytest `56 passed/0`, overall `0`.
- M0.6 negative fixture correction: `test_alarm_onset_in_masked_samples_gets_no_early_warning_credit` now places the first onset at index 110 inside failure window 100–190 and masks 110–159 (the unmasked score would be 80/90); `test_masked_alarm_carries_through_without_counting_healthy_onset` checks no new FA on unmask; NaN test executes the actual alarm policy and dwell.
- Corrected fixtures gate: `evidence/quality-gate-latest.json`, pytest `57 passed/0`, all commands and wheel smoke `exit_code=0`, overall `0`.
- M0.2 fingerprint unit gate (güncel gate): `evidence/quality-gate-latest.json`; pytest 168 passed/11 deselected, strict mypy 58 source files and all code/packaging gates `exit_code=0`, overall `0`. `vendor/__init__.py` artık fingerprint kapsamına dahildir, source-root symlink ret fixture'ı geçer. Suite/director dispatch enforcement hâlâ açık.
- Public data loader/split unit gate: `tests/test_public_data.py`; same-byte SHA verification and semicolon CSV parse, binary label/sensor separation, timestamp/revision identity, path traversal/root symlink rejection, and split axis/event-boundary behavior. Quality gate exit 0; 67 passed, Ruff/Bandit/mypy/wheel/import smoke 0, Pylint 9.87/10 (complexity warnings). All 35 real SKAB sessions passed hash and CSV loader verification and all are irregularly sampled. The current simple split was feasible for 3 sessions; 32 did not satisfy event-safe boundary/healthy train/eval anomaly conditions. This is not production time-grid materialization or public-data acceptance.
- SMD source loader: [review_smd_loader.py](review-evidence/review_smd_loader.py) loaded every one of the 28 pinned machines via production `harness.public_data.load_smd_machine`; [aggregate](review-evidence/smd-loader-check.json) confirms 708,405 official train + 708,420 official test rows and 38 sensors per machine. The loader requires the exact 60-second cadence and paper identity/hash, exposes candidate-local indices plus a canonical concatenated source index, and hashes the three consumed files (train, test, test labels). Train labels are not fabricated; interpretation-label metadata is not loaded or claimed verified. Window 100 is a loader smoke parameter only, not the 22-file TSB curated protocol: source-specific benchmark window/TSB task selection and public-data acceptance remain open.
- Data usage profile is `noncommercial_research`; Genesis is only an eligible candidate with attribution and CC-BY-NC-SA 4.0 provenance retained. The user has not granted GHL/SWaT permission; both remain excluded. The user explicitly selected SMD and revised scope to three industrial sources (GECCO, simulated CATSv2, Genesis) plus one server telemetry source (SMD). Source selection is resolved; license provenance, trusted materialization and source split validation remain open. SMD is not classified as industrial.
- Historical priority/aging GPU scheduler unit gate: SQLite-backed queue; tests cover at-most-one simultaneous lease, owner-bound ticket, priority aging, idempotent terms, 30-second bound, expiry-to-quarantine, trusted-verifier-gated recovery, queue deadline/cancel, dead-process expiry and stale-token rejection. Two real CPU processes verified that an expired live owner prevents handoff; source-bound result is `review-evidence/gpu-quarantine-review.json`. This is arbitration metadata only; it does not prove the trusted verifier's real process-group identity checks, model-process timeout enforcement, AOS wiring or real GPU coexistence.
- Sandbox live gate: pinned Python base `sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e`; built image `sha256:3f9df3002bccd1cb74aa5f6060da74dbdecc4c65fc0c8f712ecf67f0590754df`. `uv run --python 3.12 pytest -q -m live tests/test_sandbox_runner.py` exit 0, 9 passed/1 deselected. Candidate-supplied output remains untrusted; independent scorer validates numeric artifacts; labels/eval guards and prompt-injection boundary remain open.
- Crash-safe sandbox admission (historical runner revision): `review-evidence/sandbox-admission-crash-review.json` passes five exact lifecycle crash windows with a different-owner sentinel preserved and first workdir removed before second run starts. It is not bound to the current `lab/sandbox/docker_runner.py` SHA. The probe is constrained Docker/CPU only; it does not model power loss or a delayed daemon response.
- Actual PostgreSQL API/Scorer path: `evidence/postgres-scorer-integration-latest.json` captures an end-to-end synthetic run through separate Director and Scorer credentials; it confirms exact task identity, private labels, independently computed score and hash-verified report readback. It is in-process ASGI plus separate DB credentials, not a separate Scorer OS process or full Director task planner.
- Historical stop-admission quality gate: `evidence/quality-gate-latest.json`, overall exit 0; all seven recorded commands exit 0, Ruff/Bandit/wheel/import smoke pass, Pylint 9.30/10, pytest 175 passed/11 deselected. Mypy covers `harness`, `lab/referee`, `lab/llm`, `lab/sandbox`, `lab/api`, `lab/db`, `lab/director`, `lab/scorer` and `lab/cli.py` (61 source files). Runtime package versions and `uv.lock` SHA-256 are attached to the gate record. Current wheel smoke imports the packaged runner/evaluator and pinned image resource. Harness/Director trusted-source fingerprint revision is 0.13.0. Historical binding: `review-evidence/stop-admission-quality-gate-binding.json`; this gate does not establish live CLI/model/AOS acceptance.

- Queue and role review: `review-evidence/scorer-queue-review.json` passed 10/10 actual PostgreSQL enqueue/idempotency/claim expiry/reclaim/artifact-binding checks. `review-evidence/postgres-role-review.json` passed 48/48 role-authenticated grant checks with unchanged source hashes. Both use only owned synthetic fixtures, removed afterward.
- Historical finalization race: `review-evidence/task-plan-finalization-before.json` reproduced a Planner transaction reading running state, Scorer publishing a completed one-task report, then Planner appending a second task. The completed report no longer covers the stored plan. This negative evidence was resolved by explicit plan closure and database fencing: `review-evidence/task-plan-seal-review.json` passed 13/13 checks, including actual advisory-lock waiting, direct SQL append rejection after sealing, immutable seal and complete two-task report. Full multi-experiment Director orchestration remains open.

- Separate Scorer process: `evidence/postgres-scorer-process-latest.json` runs real PostgreSQL and fresh systemd worker/finalizer processes. All current tasks scored while the plan is open leaves report unavailable; an explicit Planner seal followed by the separate Scorer finalizer publishes the verified report. `review-evidence/scorer-host-admission-review.json` proves another fresh worker returns `capacity_busy` before touching an intentionally missing credential path while the host process lock is held; this is not a live DB-disconnect or GPU test.

- Baseline live calibration: `review-evidence/baseline-pipeline-review.json`, 36/36 guarded evaluations, 36 distinct Scorer worker PIDs, 3 immutable experiment/trajectory pairs and 7 end-to-end checks, exit 0; four local synthetic EVT profiles only. Harness `01d2f06369cc4ed63b5611d066be1acc82046d9c960fc5419b04ee70730f77c4`, image `sha256:7b4a99a985704ab66ff8dd39cf13db257690d20eb8f67a38d58586807be988fc`, source unchanged. Physical calibration SHA `c219a43540699eb7ffc41fa539ef32f458444415a62aee78a1fc44094f2a29cf`; equal receipt/blob digest and readback proved before owned DB fixture cleanup. The first attempt was stopped by the DB because the review driver omitted the running transition; `baseline-pipeline-driver-before.json` retains that failure, and the driver was corrected before the successful fresh run.
- Narrow baseline exception: `review-evidence/baseline-boundary-review.json`, 4/4 real Docker controls, exit 0. Ordinary constant candidate rejected; candidate environment spoof still rejected by host; changed allowlisted source rejected; exact flat robust-z reference accepted. Source unchanged; this does not recertify all historical 19 adversarial guard cases.
- M0.8 cumulative simplification component: `review-evidence/referee-cumulative-floor-review.json`, pytest 3/3 and Ruff exit 0. Repeated small losses cannot move the champion below fixed `best_suite - eps`; equality passes and the next lower float fails. This does not prove Director propagation, second-seed confirmation or full-run replay; M0.8 remains partial.
- M0.9 Director ownership component: `review-evidence/director-owner-reconnect-review.json`, 5/5 actual PostgreSQL checks, exit 0, journal SHA `7cf2afc0c86fc9baf14207c1f4b6bd57a202cfda4c20ef7e79a20d94e24d4f0a`. After an owned connection is invalidated and a replacement Director acquires ownership, the old Director cannot append a checkpoint or write blob files. Only owned fixture rows/files were removed. Full Director resume, external-action fencing and 20-proposal acceptance remain open; migration 0014 was not applied by this probe.
- Director loop preparation: `14-director-loop-review.md` records the three-seed champion-noise choice and extra validation cost. Migration 0014 applied with exit 0 (`director-event-migration-apply.json`); actual PostgreSQL journal checks 17/17, budget component checks 6/6. The six candidate scenario preflight cases passed real Docker guards on one synthetic task (`director-scenario-docker-review.json`), reusing the prior pinned image. No full Director20, current image/harness parity, independent scoring of those six cases, public-data, model or AOS acceptance is claimed; M0.9 stays partial.
- Primary orchestration fault fixtures: `director-primary-recovery-review.json` records 4/4 unit checks after fixes for retry/measurement checkpoint handling and whole-suite seed accounting; the earlier failure is retained in `director-primary-recovery-before.json`. SQL/Docker are mocked in those four cases. Separate actual PostgreSQL Planner terminal-outcome checks pass 5/5 (`planner-candidate-outcome-review.json`), including identical retry and changed-outcome rejection. Alembic metadata parity after 0014 exits 0. These component results do not close Director20 or durable full-run resume.
- Director terminal confirmation component: `review-evidence/director-confirmation-review.json`, 6/6 negative checks using synthetic measured vectors and the actual Referee/terminal preconditions. Missing seed 1, missing seed 2, foreign candidate/profile, missing guards and duplicate noise task are rejected before writing documents. This is not a SQL/Docker or full-run acceptance. `director-simplicity-review.json` separately records 4/4 source simplification checks.
- Director single-proposal live path: `review-evidence/director-vertical-review.json`, exit 0, 10/10 checks on harness `e90a583c5e02bbcfb32b9816ca78bbf136bc5b38f97734c0e0e10e58bb410a5e` and image `c230ebd5baf524422ce6caa6d1327c83e1cba02d880edfb7cb00a9b7cd487612`. A fresh 36-measurement synthetic baseline calibration feeds production `run_one_proposal`; 12 real guarded Docker/separate Scorer measurements (four tasks × seeds 0–2) produce confirmed KEEP, atomic terminal experiment/trajectory blobs, and a separately finalized hash-verified 48-score report. The driver uses a synthetic fixture and one proposal; full CLI/Director20, current-best propagation, all-phase budget/restart/stop, Git lineage, HTML/replay, public data, local model and AOS remain open. The initial readback-driver permission failure is retained separately in `director-vertical-before.json`; no runtime role grant was widened. Details and limits: `14-director-loop-review.md`.
- Completed-seed resume component: `review-evidence/director-seed-resume-review.json`, 5/5 unit checks, exit 0, unchanged measured source hashes. Measured, crashed, timed-out and constant-score-rejected seeds recover without repeat dispatch/plan/status/budget mutation; primary seed 0 can also be read after confirmation seed 1 advances the budget. `director-seed-resume-before.json` retains the original three failures. Checkpoints are an immutable in-memory adapter and SQL/Docker are replaced; this does not close actual process restart or Director20. `review_director20.py --prepare-only` created label-free synthetic suite and source-only provider inputs; no full CLI experiment was executed by that preparation. The recorded 168-test full quality gate belongs to the preceding committed vertical slice; the new loop work awaits its own complete gate.
- Production suite-manifest component: `review-evidence/director-manifest-review.json`, actual PostgreSQL/Planner role, 8/8 checks, exit 0. Four synthetic task inputs match their profiles and guard task IDs; incorrect profile hash/count/provenance, label fields and a holdout profile are rejected. Planner has no label-table SELECT grant. Loader source remained unchanged during this probe. `director-manifest-before.json` retains a review-driver error caused by immutable profile visibility; the corrected driver creates a separate owned holdout fixture. No DB protection was weakened and all owned SQL fixtures were removed. This is input/role verification only; full CLI20, public data and actual process resume remain unverified.

- Production CLI20: `review-evidence/director20-cli-review.json`, exit 0, 8/8 checks. Run `1e2bcaf6-42ee-4d3c-8d2a-26c055d8cb40` completed three real measured baselines and twenty source-only fake-provider proposals over four synthetic EVT tasks without intervention during the successful run. All four verdicts, candidate crash, 90-second test-profile fit timeout, three-seed promotions, 23 typed terminal document pairs, physical blob hashes and the separately finalized 80-score report were verified. Seven measured decisions reproduced exact verdict/delta/ci_low and prior noise from stored Scorer rows. Source unchanged; only owned SQL fixtures were removed. M0.9 passes its synthetic scope. Actual process restart/stop, real Git object/ref storage, product replay/report, public suites, model/GPU/AOS acceptance remain open. Two failed startup attempts and their causes are retained as `director20-cli-before.json` and `director20-cli-path-before.json`; neither is counted as a successful run.

- Stop admission revision 0.13.0: real authenticated ASGI/PG check 5/5 (`director-stop-admission-review.json`) and one real Docker fit → API stop → denied score boundary 3/3 (`director-phase-stop-review.json`). No provider call/checkpoint blob after an already committed stop. New image byte parity passed; full gate 175 passed/11 deselected and overall exit 0 (`stop-admission-quality-gate-binding.json`). Initial full gate exposed one legacy test lease missing the new active-state method; corrected fixture and failed evidence retained (`evidence/quality-gate-stop-before.json`). In-flight cancellation, atomic external-effect admission races, full run restart and AOS takeover remain open. CLI20 success remains explicitly bound to the archived 0.12.0 image/gate.

- Gerçek API taşıma kontrolü: `review-evidence/api-control-wire-review.json`, 21/21 ve exit 0. Ayrı Lab sunucusu, AOS Python ortamında gerçek istemci ve PostgreSQL; authenticated origin/owner/external mapping, retry/action uniqueness, süit/bütçe sınırları ve queued-stop doğrulandı. POST sonrası 0 deney/0 skor, iki özel run satırı temizlendi, kaynak hash'leri sabit. Director yürütme, typed AOS runtime, model/GPU ve public veri kabulü değildir; `15-api-director-integration-review.md`.

- API/Director 0.14.0 gate: `review-evidence/api-dispatch-quality-gate-binding.json`, exact gate SHA `3fb4aeba6605b88a35abac2b6918d19264a1671f5a1596925463d2f65d61c2a3`, harness `35fd9146eb573cee05d1d6ac2e59a7cdba12097e409e385ae92c3dbc506367b8`, imaj `bdd9733e…`. 176 test, 63 kaynak strict mypy, yedi komut exit 0. Gerçek aktif kontrol probe'u 3/3 (`api-active-controls-review.json`): health/status/ayrı kuyruk işine tekrar stop, en uzun çağrı 2,24 ms; çalışan araştırma etkilenmedi. Model/GPU veya in-flight iptal kabulü değildir.

- Gerçek API→Director araştırma koşusu: `review-evidence/api-director-wire-review.json`, 30/30, process exit 0. API'nin oluşturduğu AOS-origin run üç baseline + bir KEEP aday, 48 ayrı skor ve dört experiment/trajectory çiftiyle tamamlandı; plan ve hash-doğrulamalı rapor gerçek AOS istemcisinde okundu. 295 sağlık/status örneği; status max 2,29 ms/p95 1,84 ms. API/Director gerçek systemd limitleri 1/2 GiB, birer CPU, swap 0; peak ölçümleri yalnız bu iki sürece ait. Kaynak ve harness sabit, iki özel run satırı ve servisler temizlendi. Typed AOS runtime, public/model/GPU ve tüm host birlikte çalışma kapsamı açık.

- GPU sıra paylaşımı 0.15.0: [17-gpu-arbitration-review.md](17-gpu-arbitration-review.md). Gerçek iki systemd CPU sürecinde 11/11 kontrol, 20 dönüşümlü çalışma, son tarih incelemesinde 7/7; tüm final kaynak hash'leri sabit. İmaj `f02913da…` byte parity geçti; harness `6936fdae…`; tam gate 177 test/63 mypy kaynağı/yedi komut ve process exit 0. İlk Bandit başarısızlığı ile intermediate testler korunur. Gerçek GPU/model ve beraber yük kabulleri açık.

- 2026-09-30 fresh039 kanıt güncellemesi: [kampanya kaydı](62-fresh039-public-campaign.md). Gerçek readonly aggregate `bff089` / exit 0 ile **125/243 completed, sıfır failed**; run hâlâ running, özgün deadline korunuyor. Checkpoint R2 session 79919 / exit 0, 105 canonical kayıt ve 27 görev metadata bağı geçti; tam243 veya terminal çıkış kanıtı değildir. Bir worker journal örneği session 77740 / dış exit 0 ile iki exact metadata event topladı; explicit exit alanları yok, worker exit/PID quiescence doğrulanmadı. R1 OOM ve eksik eski provenance korunur. Birleşik041 kaynak ve test/SQL araçları hazırlanmış olsa da actual041 pytest/0035 SQL/image/gate yok. **M0.10 açık; toplam 11 geçti / 7 kısmi / 4 açık değişmedi.** Gerçek yerel model, GPU/AOS birlikte çalışma ve holdout kabulleri bu gözlemlerle tamamlanmaz.

## 2026-09-30 — 0.41.0 çalışan kurulum teslimi

- Zorunlu yedi komutluk kalite kapısı exit 0: 1247 passed, 7 skipped, 120 deselected; GPU/live hariç. [Image ve kapı](review-evidence/release041-image-gate-r4-summary.json).
- İzole gerçek PostgreSQL üzerinde 99/180/243 sentetik baseline durdurma/kurtarma ve 0035 süreç-kimliği RPC yetki testleri exit 0. [SQL kapsamı](review-evidence/release041-synthetic-pg-r3-summary.json).
- Bir gerçek Docker → Scorer → waiter exit 0 → süreç-kimliği RPC → checkpoint akışı sentetik girdilerde geçti. Önceki kayıtlar korundu, quiescence ayrıca doğrulandı. [Süreç kanıtı](review-evidence/release041-worker-process-r3-summary.json).
- Gerçek ana veritabanının izole kopyasında yükseltme/geri alma/yeniden yükseltme geçti; ardından ana kurulum 0.41.0/0035'e taşındı. Mevcut 47 tablonun kayıt/hash'leri, bütçe/deadline ve yetkileri korundu; API, konsol, Director aktif; tünel değişmedi. Konsol version 0.41.0, overview ve tamamlanmış mod projesi raporu HTTP 200. [Dağıtım](review-evidence/release041-deployment-summary.json).
- Bu teslim 22 kabul maddesinin durumunu değiştirmez: 11 geçti, 7 kısmi, 4 açık. Gerçek yerel model araştırması, holdout/eğitim ve AOS ile gerçek GPU birlikte çalışma kabulü tamamlanmadı. Başarısız fresh039 araştırması yeniden başlatılmadı veya başarılı diye sunulmadı.

- 2026-09-30 fresh039 tamamlanmış baseline kanıtı: bağımsız readonly R4 doğrulaması session 88992 / terminal `b532aa` / **exit 0**. 27 görev × 3 yöntem × 3 seed = **243** ölçüm; fiziksel çıktı/checkpoint bağları, kaynak manifestindeki ağırlıklar, 27 kalibrasyon özeti, normalizasyon/noise ve 3 typed deney/trajectory çifti özgün üreticiyle float.hex eşit. [Güvenli özet](review-evidence/public243-failed-run-baseline-proof-r4-summary.json). Önceki üç doğrulama hatası korunur. Araştırma run'ı **failed**, proposal sayısı **0**; eski worker terminal/PID kanıtı, gerçek model/GPU/AOS ve holdout kabulü değildir. Kabul toplamı değişmedi.

- 2026-09-30 izole AOS CPU lifecycle: gerçek child süreçleri/SQLite reopen/typed policy/Lab HTTP/console TCP ile eşzamanlı aynı-key start, eski lease retleri ve yeni açık yetkiyle rebind geçti. R2 barrier timeout exit 1 korunur; aynı run üzerindeki R3 devamı **10/10, exit 0**: yetkili stop tekrarı ve foreground ilerlemesi. Normal recovery apply session 92579 / `19632a` / **exit 0**; bağımsız API rapor readback `8f1b36` / **exit 0**, terminal `stopped` ve hash eşliği. [Yama ve kanıt kapsamı](review-evidence/aos042-cpu-lifecycle-summary.json). Canlı AOS değişmedi, fixture desktop/karar motoru etiketli. Orijinal dispatcher exit 1; otomatik terminal stop, gerçek Scorer inflight drain ve model/GPU birlikte çalışma açık. M0.AOS.2/.3 kısmi kalır; toplam kabul değişmedi.

## Native stopped-job expected-row negatives (source af4864d)

Owned PostgreSQL55545, yeni typed run ve ayrı instrumented source: üç
expected-row CAS negatif çağrısı P0001 ile reddedildi; sonraki normal
kapanış21 kontrol/exit0 ve bağımsız READ ONLY readback ile doğrulandı.
SQL0036/ACL/fencing değişmedi. Beş saniyelik post-claim test beklemesi
üretim zamanlama kanıtı değildir; completion snapshot'ları negatif probda
okunmadı. Failed R1–R5 kayıtları ve R5 pending deadline korunur.
[Kanıt](review-evidence/native-stop036-expected-cas.json).
Actual control-generation race, doğal expiry, native retry, shared-drain
successor ve gerçek AOS/GPU kabulü açık kalır. Goal tamamlanmadı.

Native özgün closure-expiry reddi ayrıca actual exit0 ile kanıtlandı:
doğal expiry, exact current job, gerçek Scorer invocation, diğer CAS
koşulları geçerli, erişilebilir snapshot'lar aynı. Pending R5 kaydı ve
deadline korunur; cleanup/terminal durum veya first-stop SQL guard kabulü
değildir. [Expiry kanıtı](review-evidence/native-stop036-expiry-rejection.json).
Actual control-generation race, native retry, shared-drain successor ve
gerçek AOS/GPU kabulü açık. Yeni AOS kütüphanelerinin API/wire kaynak şekli
uyumlu; authority/approval/durable HTTP effects ve ortak runtime kontrol
yüzeyi henüz bağlı değil. [İnceleme](review-evidence/aos-new-library-source-review.json).

Native terminal-row retry yeni R7 CPU fixture'ında kanıtlandı: eski running
expected-row P0001 ile reddedildi, güncel failed expected-row iki tekrarında
yetkili snapshot değişmedi. Normal commit/otomatik recovery ardından ayrı
Migrator READ ONLY readback stopped/drained/completed, tek completion/outcome,
sıfır aktif job ve eş report hash gösterdi. 21 normal kontrol, actual wait
exit0. [Kanıt](review-evidence/native-stop036-retry-proof.json). Yukarıdaki
native retry açık kaydını bu dar kanıt günceller; actual control-generation
yarışı, shared-drain successor ve gerçek AOS/GPU hâlâ açıktır. Test-only
post-claim barrier ve RPC hook üretim zamanlama kabulü değildir.

Resumed-stop sahiplik aktarımı regresyonu önce üç failure/exit1, gerçek
receipt.owner tuple'ını iki dönüş yolunda aktarınca 41passed/exit0.
Generation/deadline/SQL değişmedi; launcher exact G2 tuple'ı recovery'ye
taşır, özgün worker exit1 kaybolmaz. [Regresyon](review-evidence/resume-owner-stop-regression.json).
Actual native G1 crash→G2 resume ve etkin runtime kabulü henüz değildir.

Yeni native G1/G2 fixture'ı dar generation fence'i gerçekten kanıtladı:
exact own G1 pidfd crash, normal deadproof/drain/CAS ile canlı canonical G2,
aynı deadline/hash, gecikmiş G1 close P0001 ve değişmeyen G2 snapshot.
20 kontrol geçti; overall exit1. G2 frozen calibration önkoşulu eksik olduğu
için failed/reportNULL; controlled terminal stop/toparlanma geçmedi.
[Kapsam ve başarısızlık](review-evidence/native-controlgen036-partial.json).
Eski koşular reset edilmedi; R2 stop_requested, R3 failed; süreç/cgroup
ölümü doğrulandı, GPU release ölçülmedi. M0 kabul toplamı değişmedi.
AOS0019 artık durable HTTP journal ve optional service/UI wiring içeriyor;
expired unsent stop approval liveness kaygısı kaynakta bulundu, AOS kendi
regresyonuyla doğrulamalı. [Güncel kaynak incelemesi](review-evidence/aos-http-journal-source-review.json).
Ortak runtime/version/capability/lost-ACK reconciliation ve gerçek GPU
kabulü hâlâ açık; AOS kaynaklarına/süreçlerine müdahale edilmedi.

Aktif primary proposal stop için ayrı SQL0037/Director kaynak adayı eklendi;
SQL0031 zero-admission guard ve SQL0036 producer korunur. Son genel gate
1360passed/yedi komut exit0. Yeni ayrı PG55546 migration ve 13 native
role/ACL/guard kontrolü geçti; gerçek admitted run veya inflight cancellation
çalıştırılmadı. Ana servisler güncellenmedi. İlk gate/fixture observer hataları
korundu. [Kapsam ve kalan kabul](66-attempted-proposal-stop-candidate.md),
[Kaynak bağı](review-evidence/attempted-proposal-stop-candidate.json).
Gerçek post-calibration G1/G2 + primary stop, diğer proposal fazları ve AOS/GPU
release kabulü açık; M0 kabul toplamı değişmedi.

Yeni native kalibrasyon/primary stop denemesi gerçek süreçlerle başlatıldı;
kalibrasyon gözlem sınırında tamamlanmadı, overall exit1. Typed stop ACK sonrası
exact süreç/cgroup quiescence doğrulandı; ledger stop_requested/reportNULL,
beş partial baseline score ve sıfır aktif job kaldı. Proposal/G1 crash/G2 resume
aşamaları çalışmadı; deadline reset edilmedi. Test observer hata yolunda launcher
cleanup'ını beklemedi; R5 kaynak taslağı bunu gideriyor, henüz yürütülmedi.
[Ayrıntılar](67-native-calibration-stop-and-aos-preflight.md).
Actual AOS worker dosyaları artık mevcut; runtime_v1 preflight dirty/untracked
checkout nedeniyle admissionfalse/exit2. Wire boolean-versus-integer regresyonu
giderildi; kaynak profilleri GPU runtime admission yerine geçmez. Native terminal
stop/toparlanma, ortak capability ve gerçek GPU kabulü açık kalır.


### R5 güvenli gözlemci ve ayrı CPU fixture hazırlığı

R5 hazırlığı üretim kaynak HEAD `01dab7d97a97c4402ce416cebc6506f22834c131`
üzerinden yapıldı. Gözlemci mevcut launcher handle'larını aynı özgün kapanış
sınırında bekliyor; observation timeout süreç sonlanması sayılmıyor. Üç bağımsız
CPU waiter testi geçti. Gerçek, yalnız bu oturumun oluşturduğu systemd parent
`KillMode=process` ile exit7 verdi; kendi child'ı doğal tamamlandı, PID yokluğu
ve boş birim kontrol edildi. Bu kontrol native terminal stop veya GPU release
kanıtı değildir.

Yeni ayrı PG55547 normal dört rol/SQL0037 migration ve boş-run kontrollerini
exit0 ile geçti; dört sentetik aile kuruldu (256 train / 384 eval). Yeni istek
bütçesi 1800 saniye / 1 deney / 0 model token; eski R4 run/deadline/results
korundu. 469 dosyalı kaynak manifesti SHA256
`c0cce5c394ce5535b2c832b3edc204f89e2f64c88443b080d654587989904e59`;
izole kopyada yalnız post-calibration ve gerçek nonbaseline score-claim test
hook'ları var. Global Director P1 lock aynı inode'a bağlı, yeni tahsis otoritesi
yok. Kurulum sonrası run sayısı sıfır doğrulanarak yalnız bu PG durduruldu,
verileri korundu. Ana servisler ve AOS değiştirilmedi.

R5 native koşusu henüz başlamadı: uzun parent wrapper incelemesi,
G1 kalibrasyon → G2 resume → gerçek primary claim → typed stop → terminal
rapor/physical drain kanıtı kalıyor. AOS/GPU ortak kabul de açık.
Toplam **11 geçti / 7 kısmi / 4 açık** değişmedi. Ayrıntılı özel makine
kanıtları `data/runtime/parallel-m0/native-proposal-stop037-r5-prep` ve
`root-r5-waiter-review` altında; sırlar ve ham kayıtlar yayımlanmaz.


R5 CPU native koşusu başladı: `4f4f0112-c450-49d2-86f8-fe94b15e9179`.
2026-09-30 12:10:16 UTC readonly gözleminde running/gen1/active,
6 completed Scorer job, sıfır calibration; özgün deadline
12:36:42.576486 UTC. Parent invocation `eb422a71608c412dbe4811fba2cb9775`,
2GiB/1CPU/128tasks/swap0/2100s/KillMode=process; gerçek API ve Director
birimleri active doğrulandı. Başlangıç kapısı ana sistemde queued/current-owned
run ve queued/running Scorer job bulunmadığını doğruladı. Eski ownerless ana
satırlar değiştirilmedi. G1→G2/primary-stop/terminal-report aşamaları bu
ara gözlemde henüz kanıtlanmadı; koşu tamamlanmış sayılmıyor. SQL ölçümünde
Director rolünün raw calibration okuması 42501 ile reddedildi; mevcut migrator
rolüyle readonly gözlem yapıldı, GRANT verilmedi. Ana/AOS/GPU runtime değişmedi.


R5 son gözlem: actual exit1 / 1296.729585s / sourceunchanged. 36 baseline
score + gerçek frozen calibration, G1 crash/G2 native resume ve delayedG1
ret geçerek 22 ara kontrol tamamlandı. FakeLLM için 0 token bütçesi öneri
başlamadan budget_exhausted yaptı; primary/stop/terminalreport çalışmadı.
Ledger failed/reportNULL; budget/deadline reset edilmedi. Ayrı cleanup exit0:
exactowners dead+cgroupempty, allScorers physicallyquiescent, activejobs0;
parent/API inactive/originalPIDabsent. Yalnız ownPG stopped/storagepreserved.
Bunlar tam stop/GPU kabulü değildir. Retry invocation ancestry ve planlı
unadmitted primary completion kaynak sorunları için regresyon/SQL aday planı
sürüyor. [Ayrıntı](68-native-r5-calibration-and-budget-preflight.md),
[Makine özeti](review-evidence/native-stop-r5-partial.json). Toplam değişmedi.
# 2026-09-30 SQL0038 / native R6 güncellemesi

SQL0038 ve Python attempted-stop uygulaması yerel `4e1bb41` commit'inde;
zorunlu yedi komut exit0, 1440 CPU test geçti. Fresh native SQL0038 kurulumu
ve 36 baseline hücresi geçti; G1/G2 fencing dahil 22 ara kontrol doğrulandı.
Eski fake senaryonun `features` önerisi, ilk immutable `hparam` seçimine
uymadığından primary öncesinde reddedildi. Inflight stop ve stopped rapor
halen açık; M0 toplamı yükseltilmedi. Fixture süreçleri fiziksel kanıtla
temiz kapandı, ana runtime/AOS değişmedi. Ayrıntılar:
[Native R6](69-native-r6-provider-intent-and-cleanup.md).
# 2026-09-30 native R7 / SQL039 güncellemesi

Gerçek primary claim running iken typed stop ve tekrarlı stop doğrulandı.
Kapanış, mevcut SQL0037'de olmayan task_scores identity sütunları nedeniyle
42703 verdi. Ek SQL0039 düzeltmesi aynı gerçek kayıtlarla önce kırmızı,
sonra yeşil read-only helper kontrolüyle doğrulandı; ledger/deadline değişmedi.
Tüm işçiler fiziksel olarak kapandı; expired stop kaydı ve running job1
quarantine'da kaldı. Terminal stopped rapor ve tam lifecycle halen açık.
Son yedi komut exit0, 1444 test geçti. M0 toplamı artırılmadı.
[Ayrıntılar](71-native-r7-inflight-stop-sql039.md).

# 2026-09-30 native R8 / SQL0040 güncellemesi

SQL0039 ile 36 baseline hücresi, G1/G2 fencing ve gerçek inflight stop tekrar
geçti; v2 marker yaratıldı. Terminal kapanış Scorer → Director/Planner-only
kilit çağrısı nedeniyle reddedildi. Ek SQL0040 iki private yolu onarır; genel
yetkiler genişletilmez. Gerçek R8 Scorer receipt önce aynı hatayı üretti,
sonra mevcut özgün expired-stop guard'ına ulaştı; deadline/ledger değişmedi.
37 Scorer ve G1/G2 fiziksel olarak kapandı; quarantine korundu. Yeni SQL0040
terminal rapor kabulü henüz çalıştırılmadı; M0 toplamı artırılmadı.
[Ayrıntılar](72-native-r8-scorer-context-lock.md).

# 2026-09-30 native R9 / SQL0040 terminal kapanış

Yeni izole native CPU koşusu exit0: 36 baseline hücresi, G1 crash/G2 resume,
gecikmiş G1 retleri, gerçek running primary stop ve tekrarlı stop geçti.
V2 fullplan4 / admittedjob1 / completion4 / infrastructure_unattempted3;
recovery actor retirement, child/final seals, terminal stopped rapor ve
bağımsız fiziksel cleanup doğrulandı. Aktif job0; owned PG kapalı, storage
korundu. Owner/waiter nonzero çıkışları kayıtta korunuyor. Ana SQL0035
runtime deploy edilmedi. M0 toplamı artırılmadı; partial-CAS crash/retry,
AOS ortak admission ve gerçek GPU kabulü açık.
[Ayrıntılar](73-native-r9-terminal-stop-proof.md).

# 2026-09-30 AOS canceled-result race / broker identity

AOS kaynak incelemesindeki canceled-result yarışı önce üç yayın yolunda
başarısız regresyonla doğrulandı; dördüncü kontrol result-ready temizliğinin
atlandığını gösterdi. Çözüm immutable completed terminal/result hash yetkisini
aynı SQLite transaction içinde doğrular. Tam broker UID/process/systemd
generation kimliği ve deadline denetimi eklendi. Bounded restart dizin temizliği
marker/fairness ile çalışır; allocated no-child edge muhafazakâr biçimde açık.
Son odaklı CPU kapsamı 58 passed / 3.12s, bounded parent exit0 / 3.297917412s,
source unchanged. Bunlar fiziksel GPU/AOS ortak kabul kanıtı değildir.
AOS client/journal, tam nested schema, original admission pins ve rotation
cleanup rights halen açık; M0 toplamı artırılmadı.
[Ayrıntı](79-aos-canceled-result-publication-fix.md),
[AOS sözleşme yanıtı](78-aos-control-contract-resolution.md).

Bu düzeltmenin zorunlu kalite kapısı: yedi komut exit0, bounded parent
81.403258620s/source unchanged, 1506 passed/7 opt-in skipped/120 GPU-live
deselected, strict mypy148 dosya, wheel build/import başarılı.

# 2026-09-30 original AOS admission identity

Kalıcı stable admission binding control→broker→executor→SQLite zincirinde
uygulandı; terminal özgün server/caller/policy/source/profile/schema pinlerini
taşır. Refresh capability hash'ini değiştirebilir; eski kimlik/budget/deadline
değişmez. Legacy null authority ve değişmiş pinle adoption reddedilir. Son
peer check sırasında expiry regresyonu önce 1failed sonra fix ile geçti.
123 odaklı test / 4.19s; root parent exit0 / 4.370273320s source unchanged.
Zorunlu yedi kapı exit0, 1521 passed/7 opt-in skipped/120 GPU-live deselected;
parent83.714203130s/source unchanged. Gerçek AOS preflight exit2 dirty/untracked;
GPU/caller/schema ortak admission halen yok. M0 toplamı artırılmadı.
[Ayrıntı](81-aos-stable-admission-identity.md), [profil kaynak haritası](80-aos-profile-schema-source-map.md).

## 2026-10-01 — Gerçek AOS client/journal kaynak snapshot'u

[89-aos-reviewed-source-snapshot.md](89-aos-reviewed-source-snapshot.md):
`reviewed_snapshot` exact HEAD + tracked binary diff + selected44 + selected
untracked hash ile gerçek `/home/cachyos/aos` checkout'unu kaynak bakımından
kabul etti; CLI exit3/pending/source_ready=true, admission_allowed=false.
Temiz/commit edilmiş AOS worktree şartı kullanıcı şartı değildi; eski default
ret korunarak açık snapshot modu eklendi. External diff/textconv/clean filter
marker regresyonları önce 3failed sonra düzeltmeyle 58passed; AOS kodu çalışmadı.
Önceki [evidence socket](87-aos-evidence-socket-integration.md) ve
[bağımsız özgün bütçe](88-aos-independent-original-budget.md) kaynak teslimleri
korunur. Bütçe henüz AOS'a authenticated wire ile aktarılmıyor; fiziksel proof,
current rights/resolver, durable inference resolution, broker config ve mevcut
scheduler rezervasyonu açık. GPU/model/controlled cancellation acceptance
çalıştırılmadı; toplam **11 passed / 7 partial / 4 open** değişmedi.

## 2026-10-01 — Authenticated bağımsız bütçe aktarımı

[90-aos-authenticated-retained-budget.md](90-aos-authenticated-retained-budget.md):
aynı mevcut kontrol socket'i için açık evidence.v3 candidate; reconcile response
aynı SQLite transaction'ından unchanged evidence.v2 + independent retained-column
budget witness taşır. Original target capability ve reconcile kotası kullanılır;
yeni allocator/authority/socket yok. V2 default wire ve terminal-v1 bytes/saatler
korunur. 75 odaklı CPU kontrolü / exit0 / kaynaklar değişmeden: gerçek owned
socket+SQLite iptal/reconcile/exact retry, original budget bağları, authority/
validator rollback, byte kapasitesi ve final-encode timeout. Authentication ve
physical proof fixture'dır; actual AOS/model/GPU kabulü değildir. Deadline testi
önce 1failed sonra fix ile geçti. AOS v3 schema teyidi/client/provider/proof ve
durable resolution, broker config ve mevcut scheduler rezervasyonu bekler.
Toplam **11 passed / 7 partial / 4 open** değişmedi.

## 2026-10-01 — Actual AOS retained evidence source52

[91-aos-native-resolution-coordination.md](91-aos-native-resolution-coordination.md):
explicit release-proof48 / retained-v3 52 kaynak profilleri, exact observer
üyelikleri ve verifier/codec markerları eklendi. Actual AOS source52 immutable
beklenen HEAD/diff/selected/untracked pinleriyle CLI exit3/pending/source_ready;
admission_allowed=false. Full v3 ortak schema ve ayrı daha sıkı request schema
pinleri doğrulandı; reviewed bytes içindeki yanlış schema/duplicate JSON reddedilir.
73 portable CPU kontrolü / parent exit0 / kaynaklar sabit. Native AOS original
inference intent'ini durable çözen API yok; ACK ve receipt_recorded gate'i açmıyor.
AOS tarafında append-only resolution + explicit admission integration, trusted
physical/resolver/source providers ve ortak broker reservation hâlâ gerekli.
GPU/model/controlled recovery çalıştırılmadı; toplam11 passed/7 partial/4 open
artırılmadı.


2026-10-01 güncellemesi: `9564b284-d6f8-4297-9f62-1a58383536bd`
terminal raporlu tek gerçek S1 araştırması tamamlandı;36baseline+4aday,
model adayı DISCARD, baseline holdout passed, cleanup doğrulandı.
[Doğrulanmış teslim ve açık kabuller](105-first-completed-local-research.md).
M0.13, OM ve AOS toplam kabulleri bu sentetik tek-öneri koşusuyla kapanmaz.


## 2026-10-01 gerçek OM ve stop sonucu

[Gerçek çalışma modu ve stop kanıtı](106-mode-agent-and-inflight-stop.md):
947fe42f terminal completed, tek LSH önerisi DISCARD; holdout manual_review.
7038f5fa stop ACK ve exact fiziksel cleanup geçti, SQL terminal/rapor başarısız.
Stop kabulü açık; exit0 tamamlanma kanıtı değildir. AOS oturumuyla doğrudan
mesajlaşma kuruldu; source verifier shared absolute monotonic deadline uyumu
Scientist66 CPU testiyle doğrulandı. Güncel AOS selected source cc62d495…;
source_ready=true/admission=false. Eski b56/619b kaynak planları superseded.
Native staging GPU/admission kapalı; gerçek AOS kabulü çalıştırılmadı.


### 2026-10-01 son gerçek stop kanıtı ve AOS koordinasyonu

0ebf382a gerçek repeated inference stop: running→stop_requested→stopped,
owner/generation bağlı canonical rapor ve fiziksel GPU cleanup **geçti**.
ACK9,519ms; terminal3,080s sonra; VRAM12680MiB. Tam canceled token/wall
reconciliation **açık**;18432token/180s rezervasyon korunuyor. Önceki7038/7af
başarısızlıkları değiştirilmedi. Son mandatory gate2259/7/121 ve tüm7exit0.
[Ölçüm sınırları](106-mode-agent-and-inflight-stop.md),
[doğrudan AOS görev paylaşımı](107-aos-direct-coordination.md).
Native AOS ortak GPU/fairness kabulü **çalıştırılmadı**; actual isolated API
generation/token/context ve enabled broker rights/runnable manifest hazırlanıyor.
Bu alt kabul tüm M0/proje tamamlandı anlamına gelmez.

### 2026-10-01 kaydedilmiş kapanış kanıtı ve doğrudan AOS görev paylaşımı

Scientist V3 bir immutable kapanış gözlemi kaydetti; AOS tüketicisi kapanışı
yazamadan hata aldı. Eski kanıtın süresi doldu, observer kapandı; AOS istek
kaydı pending ve şema27 olarak korundu. Kapanış/GPU devri kabulü **geçmedi**.
İki oturum doğrudan haberleşerek ayrı, yalnız kapanışa yetkili toparlanma
sözleşmesinde uzlaştı. Scientist statik sözleşme ve salt okunur sağlayıcıyı,
AOS kendi doğrulayıcı ve atomik kapanış yazıcısını paralel hazırlıyor.
Eski kanıt, süre ve generation yenilenmez; güncel dar yetki ayrı doğrulanır.
Sonraki V2 toparlanma tanığı bir kez başlatıldı; context yazmadan exit2 verdi.
Gerçek CPU observer/GPU namespace hatası regresyonla yakalanıp dar salt okunur
kontrolle düzeltildi; makine üzerindeki before/after yokluk kontrolü geçti.
AOS tüketim sayısı0; şema27 ve pending kayıt değişmedi. Gerçek toparlanma kabulü
**geçmedi**, GPU kabul koşusu **çalıştırılmadı**. Yeni kaynak/sonlu yetki eşliği
gerekir; eski kanıt veya süre yenilenmez.
[Güncel kanıt ve görev sınırları](112-aos-retained-closure-coordination.md).

V3 gerçek toparlanma tanığı context üretti; AOS tek tüketimde exit2 ile,
migration öncesinde reddetti. Tanık özgün 60s sonunda exit3 ile kapandı;
PID/cgroup yokluğu doğrulandı. AOS DB27/pending/no receipt değişmedi.
**Kapanış geçmedi**; ret nedenini AOS oturumu inceliyor, otomatik tekrar yok.
Scientist son kaynak kapısı2781 passed ve tüm7exit0. Deney fazı11→7 ve ayrı
guard/Scorer süre kayıtları kaynak olarak tamamlandı; gerçek hızlanma, Public27
tam araştırması ve GPU kabulü henüz kanıtlanmadı.

## Ek public DEV CPU kanıtı; genel kabul açık — 2026-10-03

Gerçek arayüzden SKAB DEV çalışması tamamlandı: 283,41 saniye, 9 baseline +
3 OPTICS skoru; LSH guard reddi. OPTICS KEEP ham VUS-PR üstünlüğü değildir.
OMR/sensör görünümü ve sahipli süreç kapanışı doğrulandı. AOS gerçek GPU,
holdout, öğretmen eğitimi ve otomatik öğrenilmiş iyileşme açık kalır.
[Adımlar ve ölçüm kanıtı](120-public-dev-cpu-study.md).
