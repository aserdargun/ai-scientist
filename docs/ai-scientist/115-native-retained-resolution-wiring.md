# Native AOS çağrısının sonuçlandırma bağlantısı

2026-10-02. Önceki gerçek araştırma ve kapanış kanıtı
[114 raporunda](114-first-native-research-report.md) korunur. İlk başarılı AOS
intent'i için bağımsız retained resolution kaydı eksik olduğundan sonraki
admission reddedilmişti. Bu çalışma o bağlantıyı tamamlar. Son gerçek v7
denemesinde broker çıkarımı geçti; AOS'un sonuç kabulü başarısız oldu.
Tam ortak kabul hâlâ açıktır; son gözlem aşağıdadır.

## Kaynakta uygulanan parçalar

- Mevcut broker control socket'inden sürümlü, tek FD aktarımı. Yeni listener,
  GPU scheduler veya allocation/release işlemi yoktur. Kanal yalnız mevcut
  `read_budget` ve `verify_physical` işlemlerini taşır.
- Çağırıcının tam dokuz alanlı kimliği ve broker'ın yedi alanlı kimliği
  kullanılır. Parent PID/start, MainPID/start ile aynı olmalıdır. Her mesajda
  kernel credentials ve doğum generation'ı doğrulanır.
- Native resolver exact durable capability/reconcile ACK'lerinden sonra
  bağımsız proof bağlantısını hazırlar ve özgün AOS store/history üzerinde
  resolution ister. Kesintili adımlar otomatik yeniden gönderilmez.
- Sonuç AOS'un `ScientistRetainedResolutionResult` tipiyle döner; özgün
  intent, admission ve kalıcı resolution readback'i karşılaştırılır.
- Native launch yalnız bağımsız `retained_review_path` ve
  `retained_review_sha256` çifti varsa yeni factory'yi bağlar. AOS'ta exact
  keyword hook ve yeni kaynak pinleri olmadan Desktop başlamaz.

Factory preflight salt okunur olmalıdır; FD aktarımı veya control dispatch
yapmaz. Kapanışta sahip olunan kanal kapatılır. Bu işlem GPU kaynaklarının
bırakıldığına dair kanıt değildir.

## AOS ile eşleşen arayüz

`serve_desktop.main(..., scientist_retained_resolver_factory=None)`;
factory `verify_configuration() -> None`, `close()` ve
`factory(desktop_binding, current_binding)` sağlar. Oluşan resolver'ın
`resolve_successful(request_id)` işlemi özgün owner thread'de çalışır.
Successful receipt için client rearm yapılmaz. AOS aynı store'daki kalıcı
resolution ile typed sonucu kendi tarafında yeniden karşılaştırır.

AOS `_active_request_id` değerini özgün task/context ile birlikte tutar;
resolver öncesi ve sonrası exact request ID doğrulanır. Geç biten eski task
yeni çağrının kimliğini temizleyemez. Scientist factory de bu alanı zorunlu
tutar; tarihsel attempted listesi aktif çağrı kanıtı sayılmaz. Deadline cache'i
sayısal object ID yerine özgün context'e güçlü referans tutar; Python'ın ID
yeniden kullanımı yeni çağrıya eski süre sınırını taşımaz.

Transport descriptor SHA-256:
`cd61387214a62ddf8fbac9f063ee2154035d9848d2c15412531f850f2ddf2600`.

AOS HEAD: `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`; yeni hook yerel
değişikliktir. AOS'un özel teslim kaydı SHA-256:
`8626543d04cb0b7fde9e9a9beb2eb84bb02c85765472739d961c563a4284ca77`.
Bu dosya kaynak ve CPU teslimini kaydeder; runtime kabulü değildir.

Son aktif-request fencing teslimi SHA-256:
`c4824f23399e629545e7ef378990b462516f36dcb4cf15b210eb5ce7510ad0ed`.
Bu teslim AOS tarafında 72 CPU kontrolüyle doğrulanmıştır; önceki hook
teslimi tarihsel olarak korunur.

## Doğrulama ve kalan teslim

Scientist'in gerçek Unix IPC kullanan istemci/server eşleşme testi sekiz
CPU senaryosunu geçti: FD aktarımı, iki ardışık provider okuması,
tam kimlik hash'leri ve policy/source/schema/parent kimliği reddi.
Typed resolver'ın 21 odaklı CPU kontrolü, launch'ın 30 kontrolü geçti.
Sentetik policy/DB/generation fixture'ları gerçek GPU veya bağımsız
fiziksel kapanış kanıtı olarak sunulmaz.

Üretim factory'sinin budget/fiziksel proof bağlantısı, özgün inference
deadline/iptal sinyali ve sticky resolver cache'i kaynakta bağlandı;
23 odaklı CPU kontrolü geçti. Gerçek AOS interpreterinde Host,
BudgetVerifier, TerminalVerifier ve provider client kurulumu doğrulandı;
bu fixture bağımsız gerçek GPU kapanışını kanıtlamaz. `asyncio.wait_for` senkron
owner-thread işlemini kesemez; her socket/proof beklemesi özgün süreyle
sınırlanır. AOS bağımsız kaynak incelemesi SHA-256:
`2207789c279d604f471cb996b25dba0118c5b0ebc64a55c9d858f8241209aebf`.

Final sandbox imajı:
`sha256:e12dd9e6c660a95bf37d5db8dc9b0156b483a847a66cd712b5daf8aa0850ea85`.
Harness: `45acba391c2c32184339ae564d7bb7e0ef4e5fda247beb66bfe3c431e9cb69b9`.
[İmaj kaynak eşliği](review-evidence/retained-image-parity-20261002.json):
ağsız build exit0; varsayılan UID10001 ile 199 dosya hash'i ve broker
importları doğrulandı. Eski araştırma imajı/kanıtları değiştirilmedi.
Yeni imaj piniyle zorunlu kalite kapısı: yedi komut exit0,
2959passed/7skipped/149deselected; receipt SHA-256
`e5c10081982a4dfdea899ab041ba565962f78526379974d54c81d93591e5208a`.

APIv4 özgün 3600 saniyesi sonunda MainPID0; eski haklar yenilenmedi.
Yeni APIv5 ve başlangıçta disabled kabul paketi hazırlandı. AOS kendi v7 izole runtime
alanını sağladı; native AOS'un kendi DB/workspace ve tek console token
yazımları manifest/lifecycle kapsamına bağlanır. Scientist AOS kaynaklarını
düzenlemez. Provision readback SHA-256:
`067ab839802bf15ba65184d598cfd1c68eaf4cc7ef54696540182776af520360`.

APIv5 için yeni suite paketi CPU üzerinde gerçek exit0 ile üretildi.
[Hazırlık kanıtı](review-evidence/api-v5-suite-preparation-20261002.json)
güncel imaj/harness pinlerini, üretim materializer/registry doğrulamasını
ve değişmeyen sentetik kaynak dosyalarını kaydeder. Yeni suite:
`mode-agent-c96d01345ec4cd9188e73c8b9a867115d9240740134d382b`.
Hazırlık receipt SHA-256:
`f25545307bd7de19a667a88679ec219baf42aa367767430995c12d6c2009759c`.
Bu suite hazırlığı principal/token/generation üretmedi ve DB/model/GPU
başlatmadı. Önceki APIv4 suite'i değiştirilmedi.

Sonraki [APIv5 ve native paket hazırlığı](review-evidence/api-v5-native-preparation-20261002.json)
gerçek exit0 ile tamamlandı: yeni API generation/capability, altı eski deneyin
değişmeyen satırları ve kaynak039'un kapalı kalması doğrulandı. Provisioning
receipt SHA-256:
`2a0680d9168c071189c636c6b4d4c9da78df6010e0cf56acdd6b5de6f00d0a72`.
API çağrıları ve servis ömrü özgün BOOTTIME bitişine bağlıdır; suspend veya
gecikmeli aktivasyon süreyi yenilemez. AOS'un kaynak kabulü içerikten
doğrulanır. Bu iki düzeltmenin 15 hedefli çevrimdışı kontrolü exit0 geçti.

Özgün disabled native paket 67 Scientist ve 73 AOS olmak üzere toplam
140 kaynak pini, 13 config ve yeni v7 runtime kapsamını bağladı. Bu
hazırlık kanıtı değişmeden korunur. Paket receipt'i:
`122cf34ec38743ac225b464cfd1847f64b90f3c03307476001b5f2cba946877f`.
Mevcut açılışta boş ortak GPU grubunun 16 GiB RAM / swap0 / CPU%200 /
128 süreç sınırları kernel üzerinden doğrulandı; scheduler24done değişmedi.
Bu hazırlık yeni model veya GPU işi başlatmadı; CPU clone ve deney kayıtları
ayrı sahiplikte korunur.

[Native v7 promotion kanıtı](review-evidence/native-v7-promotion-20261002.json)
karşı incelemenin istediği `src/aos/desktop_console.py` pinini ekler:
`ff36943ac9e8da884339a05969d2e7b602d54c4df8fc29e2085ff10c8085c773`.
Güncel paket 67 Scientist ve 74 AOS olmak üzere **141 kaynak** bağlar;
bağlı hash/receipt'ler yeniden üretildi ve yedi çıktı dosyasının hash'i
doğrulandı. Gerçek `ControlPolicy.verify` ve canlı API'ye salt okunur
`JointLabCapability` kurulumu exit0 ile geçti. Promotion receipt SHA-256:
`4bffd1c95d22a47a7e00e153e52ae1b331560c8c62047f3e2774195287514008`.

İncelenen paket artık enabled durumdadır. Bu promotion AOS kaynak yazımı,
servis veya model başlatma ya da GPU edinimi yapmadı. Doğrulama anında
APIv5 aktifti; özgün `CLOCK_BOOTTIME` bitişi `5380.211217774` korundu,
süresi yenilenmedi. Native açılış koruması ve özgün yetki süresi içindeki
çalıştırma henüz tamamlanmış kabul olarak kaydedilmez.

Scientist tek GPU yürütücüsü olarak tekrarlı adil ilerleme ve ayrı kontrollü
iptal/toparlanma kabulünü çalıştırır. Bu iki kabul halen açıktır. Public
veri, öğrenilmiş adaptör, lisans seçimi ve genel CI kabulü bu belgeyle
tamamlanmış sayılmaz.

## Son gerçek v7 denemesi: çıkarım geçti, sonuç kabulü başarısız

[Deneme ve kapanış kaydı](review-evidence/native-v7-retained-acceptance-failed-20261002.json)
`f43b5fa8a7944bf39cb46c7a91456228` pipeline'ının gerçek exit1 sonucunu tutar.
Native açılış ve aynı canonical scheduler üzerindeki fence25 AOS çıkarımı
geçti. AOS'un ilk dosya görevi, `post_intent_authorization` aşamasındaki
`ScientistAdmissionError` nedeniyle başarısız oldu. `attempted=false`:
retained control IPC'ye gönderilmedi; aynı istek yeniden gönderilmedi.
Scientist v5 deneyi, ikinci AOS görevi ve rapor doğrulaması başlamadı.

| Ölçüm | Sonuç |
| --- | ---: |
| GPU kuyruğu → tahsis | 25,873 ms |
| Model yükleme | 3.628,647 ms |
| Model çıkarımı | 963,320 ms |
| Tahsis → terminal broker receipt | 8.118,531 ms |
| Başarısız AOS görevi toplamı | 42.018,179 ms |
| Örneklenen tüm cihaz VRAM tepesi | 4.022 MiB |
| İşçinin raporladığı VRAM tepesi | 3.811.927.040 bayt |

Model: yerel Decider `Qwen3_5ForCausalLM`, BF16, quantization yok;
istenen context16.384, çıktı sınırı512. Scientist Qwen3.5-9B/FP8 bu
denemede çalışmadı. 906 cihaz örneğinin tepesi, kesin işçi tepesi olarak
sunulmaz. Scientist'e devir ve deney gecikmesi ölçülmedi.

Tek özgün üç saniyelik control içinde tekrarlanan tam kaynak/runtime
doğrulamaları süre tükenmesiyle tutarlıdır. Eski AOS logu en iç hata
konumunu kestiğinden kesin leaf exception kaydı yoktur. Scientist tarafında
aynı control içinde doğrulayıcı süreç açılışlarını azaltan düzeltme uygulandı.
Canlı policy/iptal/owner/generation kontrolleri ve özgün süre
korunur; yukarıdaki tarihsel 2959 test kapısı yeni düzeltmenin kapısı değildir.

Fiziksel kapanış bağımsız doğrulandı: işçi PID/cgroup yok, unit MainPID0;
broker'ın fiziksel drain ve `release_outcome=released` kaydı aynı request'e
bağlı. Kendi AOS/broker servisleri kapandı ve masaüstü konteyneri kaldırıldı.
APIv5 MainPID0; bize ait PostgreSQL konteyneri exit0 ile durdu, konteyner ve
veritabanı korundu. Eski koşular uzatılmadı veya yeniden sonuçlandırılmadı.
Kullanıcının AOS arayüzüne, Scientist arayüzüne ve tüneline dokunulmadı.

Model cleanup SHA-256:
`195f514fca72e99f7f3f5e565ace1b6abeffa240d28d967f3e2b6ed8dd2438cf`.
Native cleanup SHA-256:
`dc441d87f554c02b8f6ee7c29b63cd7837216321dbe9d16ca7ec89e7a4297b26`.
API/clone cleanup SHA-256:
`27c2f6d3400873affa79264d8d357574b2e22e5430da9b2a5a181d4402e52121`.

### AOS oturumuna kısa aktarım

Scientist HEAD `3988a1936455f0d9aebe3c68110a6a7d56d32d04`, AOS HEAD
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`; gerçek koşu 141 kaynak piniyle
bağlıydı. Sonradan alınan yerel fark hash'leri raporda ayrı tarih taşır;
koşu öncesi tam worktree diff'i olarak sunulmaz. Sözleşme/schema hash'leri
raporda kayıtlıdır. AOS kaynak yazımı Scientist tarafından yapılmadı.
Canlı kaynak dondurma süreci sona erdi; eski v7 intent/API/artifact yetkileri
yeniden kullanılmayacak. Scientist düzeltmesi ve AOS hata-konumu teslimi
eşleştirildikten sonra yeni generation gözden geçirilecek. Tek GPU yürütücüsü
Scientist kalır. Adil tekrarlı ilerleme ve kontrollü inflight iptal/toparlanma
ayrı, henüz çalıştırılmamış kabul maddeleridir.

## Özgün kontrol süresini koruyan kaynak düzeltmesi

`aos_native_artifact_receipts.py` aynı kontrol içinde doğrulanmış dosya
baytlarını, her kullanımda güncel yol/FD kimliğini yeniden denetleyerek
paylaşır. Her interpreter için yalnız bu kontrol süresince yaşayan bir
metadata alt süreci kullanılır. Bağımlılık keşfi ve sürümler her istekte
yeniden okunur; standart paketlerde yalnız gerekli Name/Version başlıkları
ayrıştırılır. Kontrol biterken sahip olunan bütün alt süreçlerin kapanışı
denenir; cleanup hatası başarıya çevrilmez.

`aos_native_retained_factory.py` yeniden okunan aynı schema baytları için
deterministik Codec doğrulamasını aynı kontrol içinde paylaşır. Her adımda
kaynak/config okuması, canlı yetki, iptal, owner/generation ve artifact
kontrolleri sürer. Cache başka kontrol veya thread/task'e taşınamaz; eski
istek, süre veya yetki yenilenmez. Sözleşme sürümleri değişmedi: runtime.v1,
control.v1, retained evidence wire.v3 ve retained channel.v1.

83 odaklı CPU kontrolü geçti; bir native constructor testi bu odaklı
komutta hariç tutuldu ve tam kalite kapısında yer alır. Gerçek AOS
interpreteri ve kurulu dosyalarla yapılan **CPU fixture** ölçümünde 10 tam
factory callback / 30 artifact doğrulaması / 60 snapshot, son kodun bağımsız
root tekrarında **2,728855 saniye**, exit0 ile tamamlandı. Üç saniyelik
sınır değişmedi. Son Codec paylaşımı öncesinde aynı bileşim 3,488243 saniye
sürüyordu; özgün sınırda sekizinci callback sonrasında reddediliyordu.

Ölçümde kapalı servislerin kimlik/yetki bağlantıları fixture ile temsil
edildi; geçici policy ve receipt canlı yetki vermez. SQLite/IPC maliyeti,
gerçek retained resolution ve yük altında ortak kabul ölçülmedi. Yaklaşık
271 ms kalan süre, her yük altında başarı garantisi değildir. CPU ölçüm
kaydı SHA-256:
`26e2c18497d8fbbd2924445fc4949c1b83d76e6d16dbc2e2feadb2671238c74d`.

İmaj ve harness içeriği değişmedi: güncel harness yeniden hesaplanarak
`45acba391c2c32184339ae564d7bb7e0ef4e5fda247beb66bfe3c431e9cb69b9`
doğrulandı. Yeni model veya imaj build'i gerekmedi. Tam kaynak kalite
kapısının ilk koşusunda altı komut geçti; pytest'te 2996 geçti, tek disk
rezerv kontrolü `/tmp` toplam kapasitesi 20 GiB'den küçük olduğundan reddetti.
İkinci koşuda seçilen uzun geçici yol Unix soket sınırına takıldı. Kısa,
özel bir geçici yol ve pytest basetemp'i proje diskinde seçildi; etkilenen
32 kontrol geçti. Üretim rezervi veya soket güvenliği değiştirilmedi.

**Bu teslimin tam kalite kapısı: yedi komut exit0; 2997 passed, 7 skipped,
149 deselected.** Pytest 216,85 saniyede tamamlandı. Son gate SHA-256:
`75cae797aeee547cc18e7994101fabedcdbb1e0fa123eb1bd56c4d1875638338`.
[Kaynak hash'leri, CPU ölçümü ve teslim sınırları](review-evidence/native-retained-control-scope-20261002.json)
başarısız ortam koşularının hash'lerini de korur. Gerçek GPU kabulü yeni
kaynak/config pinleriyle henüz çalıştırılmadı; eski runtime paketleri
güncel kaynakla kullanılabilir sayılmaz.

## Tüm retained yaşam döngüsünün süre düzeltmesi — 2026-10-02

`fb53bea` sonrasındaki inceleme, ilk control düzeltmesinin kalan zinciri
kapsamadığını gösterdi. Gerçek AOS interpreteriyle CPU bileşiminde provider
hazırlığı özgün 3 saniyede, sonuç doğrulaması ise özgün 30 saniyede süreyi
aşıyordu. Aşamalar birleştirildiğinde 106 doğrulamanın 99'unda 30,001 saniyeyle
reddedildi. Bu başarısız ölçümler özel kanıt dosyalarında korunur.

Scientist factory artık ön kontrol, control, provider hazırlığı ve sonuç
doğrulamasında ayrı işlem kapsamları kullanır. Dış çözümleme kapsamı yalnız
özgün 30 saniye içinde aşamalar arasındaki tekrar maliyetini azaltır; iç
control/provider süreleri hâlâ 3 saniyedir. Hiçbir aşama süreyi yenilemez.
Her callback kaynak/config dosyalarını, güncel yetkiyi, owner/generation'ı,
iptali ve bağımlılıkları yeniden denetler. Immutable dosya baytları yalnız
güncel yol/FD kimliği aynıysa paylaşılır; kapsam başka task/thread'e geçemez.

Metadata alt süreci yeni sürüm sorgusunu hesaplarken aynı snapshot'ın kaynak
dosyaları denetlenir. Gönderim ve yanıtın alınması aynı özgün 2 saniyeyi
paylaşır; bekleyen ikinci istek kabul edilmez. Doğrulama hatası ve iptal
sahip olunan sürecin kapanışıyla sonuçlanır. Toplam 32 MiB kaynak sınırı
kalan bayt bütçesiyle her okumadan önce uygulanır.

Son kodla **106 doğrulama, 636 taze snapshot ve 16 native Codec kurulumu
23,915995 saniye/exit0** ile tamamlandı; CPU bileşiminde yaklaşık 6,084 saniye
pay kaldı. Capability/reconcile 2,274'er saniye, provider hazırlığı 1,576
saniyedir. 88 odaklı kontrol geçti. Bu gerçek AOS interpreteri üzerinde
**CPU fixture** kanıtıdır: servis yetkileri, store, kanal ve host dispatch
fixture'dır; canlı SQLite/IPC/GPU veya yük altında gecikme kabulü değildir.
Tam kalite kapısında **3002 test geçti; yedi komut exit0**. Dondurulmuş kaynak hash'leri
[yaşam döngüsü teslim kaydına](review-evidence/native-retained-lifecycle-scope-20261002.json)
yazıldı. Model, harness, imaj, AOS kaynağı ve ortak protokol sürümleri değişmedi.

Yeni APIv6 süiti dosya düzeyinde hazırdır: aynı tek sentetik snapshot,
bir öneri, en çok 900 saniye ve 30.000 model tokenı. AOS'un ayrı v8 alanındaki
11 boş dizin, iki henüz oluşturulmamış veritabanı yolu ve 74 kaynak hash'i
salt okunur doğrulandı. Eski APIv5/v7 yetkileri taşınmaz; yeni servis, token,
generation veya yetki saati bu hazırlıkla başlatılmaz. Gerçek retained
resolution/ikinci görev, çekişmeli GPU devri ve ayrı kontrollü iptal/toparlanma
yeni kaynak/config anlaşmasından sonra tek Scientist yürütücüsünde açıktır.
