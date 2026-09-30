# Yeni 0.39 public veri kampanyası

2026-09-30. Ana 0.39 kaynak/imaj/gate bağlarına dayanan ayrı kampanya
yeni DB ve calibration kimliğiyle kuruldu. Gerçek init **exit 0 / 3,097
saniye**, admit **exit 0 / 42,505 saniye** verdi. Özgün 056 kaynak fence’leri
ve yeni kaynak hashleri öncesi/sonrası aynı; eski 135 ölçüm yeni koşuya
yeniden etiketlenmedi.

Admission 27 public development görevini kaydetti; development/holdout
ve kaynak üyeleri ayrık, Director’ın özel veri okuması reddedilmiş olarak
kanıtlandı. Kullanım profili ticari olmayan araştırmadır. Ham profiller,
özel satırlar, DSN ve holdout sayısal sonuçları bu belgeye alınmaz.

[Hashlere bağlı init/admit özeti](review-evidence/fresh039-init-admit-summary.json).
135 hücrelik yeni calibration başlatıldı; bu belgede tamamlanma veya skor
kanıtı yoktur. Daha sonraki araştırma için 243 baseline hücresi planı
başarılı araştırma sonucu değildir. Calibration ve araştırmanın özgün
14.400 saniyelik bütçeleri korunur; tekrar başlatma süreyi yenilemez.

Provider **`fake-json`**; yerel LLM veya GPU başlatılmadı. Yeni bilimsel
araştırma, holdout, gerçek model/GPU, AOS birlikte çalışma ve M0.10
kabulü bu iki aşamayla tamamlanmış sayılmaz.

## İlk salt okunur ilerleme gözlemi

Root gözlemcisi gerçek exit 0 verdi (`bade06`): 135 beklenen hücrenin
33’ü kaydedilmiş, 33 claim başarılı, bir claim çalışıyor ve başarısız
claim sayısı sıfırdı. Kontrol durumu `running`, rezervasyon 3.400 saniye;
ilk deadline `2026-09-30T04:14:23.126375+00:00` ile aynıydı.

Bu, ayrı Scorer rolüyle repeatable-read/read-only transaction içinde
alınan toplam sayılardır; skor değerleri ve holdout satırları seçilmedi.
Gözlemci SHA-256:
`c3722a433e623cc38ddc82bbc989a2425a070ad9d46735e8482ad84a6d861305`.
Tam kalibrasyon ve bağımsız 135 hücre doğrulaması henüz tamamlanmadı.


## Calibration iç sonucu, dış hata ve register teşhisi

135 hücrelik yeni calibration’ın iç aşaması `complete`, child ve özel
phase receipt exit **0** döndürdü. Dış systemd unit’in gerçek exit kodu
**120**; bu başarısız dış sonucu iç exit 0 ile değiştirmiyoruz. Özgün
özel journal ve receipt korunur. Python, interpreter cleanup/stream flush
hatasının exit kodunu 120 yapabileceğini belgeler; bu koşudaki neden
**doğrulanmadı**, yalnız olası açıklamadır.
[Python sys.exit belgesi](https://docs.python.org/3.13/library/sys.html#sys.exit).

Sonraki register çağrısı gerçek **exit 1 / ValueError** verdi;
`actual135-proof039.json` oluşturulmadı. İlk teşhis yardımcı dosya yolu
hatasıyla başarısız oldu; sonradan çalışan `cat` komutunun exit 0 sonucu
teşhis başarısı sayılmaz. Düzeltilmiş R2 ve R3 teşhislerinin systemd exit
kodları ayrı ayrı **0** ölçüldü.

R3 salt okunur kontrolü 135 joined hücre, 135 farklı claim ve 15 summary
buldu. Bilimsel job imajı tam 64 hex karakter, runtime pini `sha256:`
önekli aynı digest: dört biçim/eşlik kontrolü geçti. Guard hatası bu
metinsel biçim farkı olarak doğrulandı; imaj veya eski ölçüm kayıtları
yeniden pinlenmedi. Teşhis registration çağırmadı, skor/özel değer
yayımlamadı. Worker/generation ve summary aritmetiğinin tam yeni proof’u
ve başarılı registration henüz kanıtlanmış değildir.

[Başarısızlık ve teşhis hash özeti](review-evidence/fresh039-calibration-register-diagnosis-summary.json).
Ayrı continuation sürücüsü yalnız bilimsel digest karşılaştırmasını
normalize etmek üzere kaynak olarak hazırlandı; runtime tam pin eşliği
korunur. Henüz çalıştırılmadı. Eski driver/context/helpers, phase
başarısızlıkları, özgün deadline ve bütçeler korunur; yeni araştırma veya
M0.10/bilimsel/GPU/AOS kabulü tamamlanmış sayılmaz.


## Ayrı V3 registration ve sonraki aşama bağları

Ayrı V3 continuation ile gerçek register **session 83854 / dış exit 0**
ve iç receipt exit 0 verdi. 135 ölçüm / 15 task için worker/generation
bağları ve summary aritmetiği binder’dan önce doğrulandı. Üretilen
`actual135-proof039.json`, başarılı yeni register receipt’inin proof
sonucuyla birebir aynı. Yalnız bilimsel digest karşılaştırması normalize
edildi; runtime tam pini, kaynak/context ve özgün deadline/bütçe değişmedi.

İlk calibration dış exit 120, ilk register exit 1 ve teşhis hata geçmişi
korunur. V3 setup’ın mevcut log/ready guard’ında exit 1 vermesi de ayrı
başlatma hatasıdır; sonraki gerçek registration’ın exit 0 sonucuyla
silinmez. [Hashlere bağlı V3 registration özeti](review-evidence/fresh039-register-v3-summary.json).

Research-admit için on zorunlu bağımlılık hazırlanır: eski init/admit/
calibrate iç receipt’leri, özgün prepared manifest, başarılı **yeni V3**
register receipt’i ve proof/registry/scenario/request/principals dosyaları.
Eski driver yalnız ilk üç aşamada kabul edilir; yeni register ve sonraki
receipt’ler V3 driver hashine bağlıdır. Research için ayrıca başarılı yeni
research-admit receipt’i ve admission dosyası gerekir; onun hash ve yeni
run kimliği root tarafından bağlanır. Template dosyaları yetki değildir.

Bu kayıt anında araştırma admission/dispatch çalıştırılmadı; bütçe veya
deadline yenileme planlanmaz. Provider `fake-json`; registration başarılı
olması gerçek LLM/GPU, araştırma/holdout, AOS birlikte çalışma veya
M0.10 kabulünün tamamlandığı anlamına gelmez.


## Gerçek yeni araştırma admission

Sonraki research-admit **session 19699 / dış exit 0**, iç exit 0
verdi (yaklaşık 1,165 saniye). Üretim in-process ASGI yolu yalnız bir
yeni, yeniden kullanılmamış `queued` run oluşturdu. Yeni run kimliği
phase receipt’iyle aynı; campaign sahibi, provider `fake-json` ve kaynak
fence’leri korundu. [Redakte edilmiş admission özeti](review-evidence/fresh039-research-admission-summary.json).

Research template artık on iki bağımlılık, admission hash ve yeni run
kimliğine bağlıdır; hâlâ kendi başına yetki vermez. Dispatch bu kayıt
anında çalıştırılmadı. Root’un ayrı onayı, kalıcı stdout/stderr günlükleri
ve en az 12 GiB kullanılabilir RAM / 20 GiB boş disk guard’ları gerekir.
Özgün 14.400 saniyelik araştırma bütçesi korunur; süre yenilenmez.
Başarısız önceki aşamalar korunur; admission exit 0 başarılı araştırma
veya bilimsel/GPU/model/holdout/AOS/M0.10 kabulü değildir.


## Araştırma başlatıldı; sonuç henüz yok

Yeni run için gerçek research dispatch **session 2280** ile başlatıldı;
son ölçülen unit durumu aktif/çalışıyor (`642cfe`). Tamamlanma veya terminal
exit sonucu henüz yok. Stdout/stderr kalıcı, append modunda `0600` özel
günlüklere yazılır; `--pipe` kullanılmadı.

Parent unit 2 GiB RAM / 1 CPU / 128 task / swap sıfırla sınırlı. Dış
unit 15.150, iç phase 15.120, child 15.060 saniye ile çevrilidir; run’ın
özgün **14.400 saniyelik** bütçesi genişletilmez veya yenilenmez. Başlatma
öncesi 19,8 GiB kullanılabilir RAM ve 64,9 GiB boş disk ölçüldü; 12 GiB
RAM / 20 GiB disk guard’ları geçti. Paylaşılan CPU kilidi araştırma
tarafında tutulur; başka ağır gate/test başlatılmaz.

[Redakte edilmiş başlatma özeti](review-evidence/fresh039-research-launch-summary.json).
243 baseline hücresi planı henüz tamamlanmış ölçüm değildir. Provider
`fake-json`; bilimsel araştırma, gerçek LLM/GPU, holdout, AOS birlikte
çalışma ve M0.10 kabulü açık kalır. Önceki dış calibration 120 ve
register/teşhis/setup başarısızlıkları korunur.


## İlk salt okunur araştırma ilerleme snapshot’ı

Sınırlı Scorer aggregate observer gerçek **exit 0 / 0,366 saniye** verdi.
Anlık snapshot: run `running`, **8/243** baseline hücresi, sekiz completed
job, sıfır failed ve o anda sıfır running job; başka evaluation hücresi
yok. Job sayısı snapshot anını gösterir; hücreler arasındaki sıfır running
job araştırmanın bittiği anlamına gelmez.

Özgün SQL execution contract hash’i
`cf0a370b1c7f635a8f596bf6c473da7abe59c7923041ae94b2030ff369f06d45`,
deadline `2026-09-30T04:59:58.870241+00:00`; run wall sınırı 14.400 saniye.
Observer repeatable-read `READ ONLY` çalıştı; skor, holdout satırı veya
ham budget checkpoint seçmedi. [Redakte edilmiş snapshot](review-evidence/fresh039-baseline-progress-summary.json).

Bu ilerleme tam proof, tamamlanmış 243 ölçüm veya terminal exit kanıtı
değildir. Tamamlanma ayrıca gerçek unit/invocation journal exit kaydı ve
son doğrulamayla bağlanmalıdır; run yeniden başlatılmaz. Bilimsel/GPU/
model/holdout/AOS/M0.10 kabulü açık kalır.


## Bağımsız135 salt okunur ölçüm kanıtı

Ayrı Scorer `REPEATABLE READ READ ONLY` doğrulayıcı R4 gerçek **dış exit0**
verdi (session39840). Tam **135 hücre /135 claim /15 görev** matrisinde
15×3 yöntem×3 seed, baseline kaynak/profile/harness/image eşleşmeleri,
sonlu ölçümler, bağımsız hesaplanan ortalamalar ve eşit1/15 ağırlıklar
geçti. Özgün kalibrasyon bütçe/deadline’ı ve başarılı V3 registration
artifact eşitliği korundu. Bilimsel image bare64 ile runtime `sha256:`
pin’i aynı digest’i temsil ediyor; yeni ölçüm, repin veya süre yenileme yok.

İlk R1 helper’ın journal’ı tek JSON sanması başlatmadan tespit edildi.
R2 gerçek dış exit1/TypeError verdi ve failure receipt’i korundu:
SQL `worker_start_ticks` alanı `String(32)` iken helper integer karşılaştırıyordu.
Diagnostic-only R3 başlatılmadı. R4 yalnız doğrulanan pozitif ASCII digit-string
veya tam integer’ı yerel integer’a çevirip kernel nesliyle karşılaştırır;
SQL metadata değişmez. Hata trace’i yalnız function/line/type içerir.

Kalibrasyonun iç phase ve ürün CLI’si exit0/135 complete olsa da özgün
**dış unit exit120** journal kanıtı korunur; dış başarı diye etiketlenmez.
135 kayıtlı worker’ın PID/start-ticks/boot/cgroup nesli artık quiescent;
GC veya kayıp unit exit0 sayılmaz. **Bireysel worker exit0 receipt’leri
ayrıca doğrulanamadı** ve açık kalır. Quiescence ile gerçek çıkış sonucu ayrıdır.

[Skor ve ham process kimliği içermeyen bağımsız135 özeti](review-evidence/fresh039-independent135-summary.json).
Bu kanıt 243 development baseline, koşullu15-task holdout, gerçek yerel
LLM/GPU/AOS birlikte çalışma veya tam M0 kabulünü tamamlamaz.

## Paralel hazırlık ve 96 hücre gözlemi

Salt okunur aggregate observer gerçek exit 0 verdi (`9e06cb`, 0,470 saniye,
47,7 MiB bellek tepe değeri). Snapshot **96/243 completed hücre, sıfır failed**
ve iki yöntemde ölçüm gösteriyor. Run hâlâ `running`; özgün contract hash’i
ve deadline değişmedi. [Yeni snapshot](review-evidence/fresh039-baseline-progress-96-summary.json)
önceki sekiz hücrelik gözlemi silmez.

Checkpoint extractor R1 ayrı 256 MiB cgroup içinde gerçek dış exit 1 verdi;
exact invocation journal sonucu `oom-kill`, süreç sonucu killed/9.
72.330.055 baytlık manifestin bütünüyle JSON yüklenmesi kaynak incelemesinde
bellek sorunu olarak belirlendi. Receipt üretmedi; başarılı kontrol sayılmaz.
Araştırma unit'i aynı invocation ile çalışmaya devam ediyor. Manifesti
stream hash ile doğrulayan ve yalnız admitted görev metadata’sını okuyan
R2 hazırlanıyor; henüz runtime kanıtı yok.

Üç paralel eylemci checkpoint doğrulamasını, gelecek sürümün gerçek süreç
kimliği kayıtlarını ve birleşik test planını hazırlıyor. Çalışan 039 kaynakları,
veritabanı ve bütçesi değiştirilmez. Ağır testler CPU kilidi serbest kaldıktan
sonra çalıştırılır. Kaynak hazırlığı gerçek PostgreSQL, worker exit veya
sürüm kabulü değildir; M0 durumu **11 geçti / 7 kısmi / 4 açık** kalır.

## Checkpoint R2 gerçek kontrolü

R2 root incelemesinden sonra 256 MiB / 0,25 CPU / 64 task / sıfır swap
sınırında çalıştırıldı. **Gerçek dış exit 0, session 79919 / `8ef094`**, süre
7,668 saniye ve bellek tepe değeri 168,5 MiB. Manifest yalnız 1 MiB
parçalarla hashlenir; feature dizileri JSON olarak yüklenmez. Mevcut Scorer
rolünün kompakt görev metadata’sı ve Director rolünün execution contract,
checkpoint ve development metadata’sı ayrı repeatable-read `READ ONLY`
transaction’larda doğrulandı. Yeni grant, heartbeat veya admin erişimi yok.

27 admitted görev ve **105 canonical checkpoint** kaynak/profile/harness/
manifest bağı geçti. 105 kayıtta eski `exit_code=0` ve beklenen unit alanları
var; exact invocation alanı sıfır, recovered kayıt sıfır. Bu alanlar ayrı
worker journal terminal kanıtı değildir. `complete243=false` ve
`terminal_worker_journal_verified=false` açıkça korunur.
[Skor içermeyen gerçek R2 özeti](review-evidence/fresh039-checkpoint-index-r2-summary.json).
Özgün R1 OOM kaydı korunur; araştırma aynı invocation ile çalışmaya devam eder.

Gelecek birleşik sürüm ayrı `release041/source` kopyasında hazırlandı:
306 runtime dosyası, çakışmasız yedi süreç kayıt payload’ı; 040 stop donor’ı,
039 koşusu ve ana kaynak envanterleri değişmedi. AST ve Ruff geçti.
VERSION hâlâ 0.39.0; henüz gerçek pytest/0035 PostgreSQL/image/gate kanıtı
yok. 0034 stop ve 0035 provenance düzeltmeleri gerekli gerçek kapılar
geçtikten sonra tek 0.41 sürümü olarak değerlendirilecek.

## 125 hücre ve odaklı test kapsamı

Aggregate observer gerçek **exit 0 / `bff089`**, 0,528 saniye verdi:
125/243 completed baseline hücresi, sıfır failed, iki yöntemde ölçüm;
başka evaluation hücresi yok. Run `running`, deadline ve contract hash’i
önceki gözlemlerle aynı. Özgün research launcher session 2280 yeniden
poll edildi (`4d5c76`); canlı handle ve aynı unit invocation korunuyor.
[Skor içermeyen 125 hücre özeti](review-evidence/fresh039-baseline-progress-125-summary.json).

041 odaklı test runner R1 başlatmadan reddedildi: üç test dosyasına
172 vaka şartı koyması, gerçek 040 kanıtının dört dosyasını kapsamıyordu.
R1 değişmeden tutuldu. R2, önceki dört dosyayı ve iki yeni 041 dosyasını
çalıştıracak. Önceki 124 gerçek vaka + yeni dosyalardaki AST ile sayılmış
27 ve 21 vaka = **172 beklenen vaka**; henüz pytest collection veya
birleşik test başarısı değildir. R2 salt kaynak incelemesinden geçti;
CPU kilidi ve 2 GiB/1 CPU/sıfır swap sınırlarıyla gerçek testini bekliyor.
Bu hazırlık M0 kabul sayılarını değiştirmez.

## Örnek worker journal kanıtının sınırı

Mevcut Scorer rolünde `READ ONLY` sorgu, tamamlanmış bir baseline işini
immutable task-score invocation’ı ve iş tuple/attempt’i ile bağladı.
Job UUID’den türetilen exact unit ve invocation için yalnız whitelist journal
metadata’sı okundu; MESSAGE ve skorlar seçilmedi. Sınırlı helper gerçek
**dış exit 0 / session 77740 / `29d0fb`** verdi.

İki eşleşen journal event’inde **EXIT_CODE, EXIT_STATUS ve UNIT_RESULT yok**.
Dolayısıyla `exact_normal_exit0_verified=false` ve
`worker_PID_quiescence_verified=false`. Helper’ın başarılı metadata toplaması
gerçek worker çıkış başarısı değildir. [Redakte edilmiş örnek sonuç](review-evidence/fresh039-worker-journal-sample-summary.json).
Eksik kayıt için process memory erişimi, journal yapılandırma değişikliği,
admin rolü veya tekrar araştırma başlatma kullanılmadı.

243 matrisi için memory-safe R3 ve terminalde kullanılabilecek journal
collector kaynakları hazır; tam matris yeni 243-record checkpoint index’i ve
korumalı completion artifact’i gerektirir. 105 kayıtlık önceki index yeterli
değildir. Ölçüm matrisi ve süreç kanıtı ayrı raporlanır; mevcut eski kayıtlar
yeniden etiketlenmez. 0035 gerçek PostgreSQL rehearsal yardımcılarının bağımsız
kaynak incelemesi tamamlandı; asıl 040 SQL kanıtı ve CPU kilidi bekleniyor.

## Olağan worker çıkışı ve kalibrasyon süreç kimliği

Bağlayıcı spec §3.2.8 / M0.9, brief ve review eki kaynakları yeniden
incelendi. Olağan Scorer için ayrı süreç, doğru job/unit/invocation/attempt,
gerçek fresh waiter exit kodu ve immutable kayıt bağı gerekir. Gelecek041
düzeltmesi bunları stdout kimliği → gerçek waiter sonucu → dar0035 RPC →
canonical checkpoint üzerinden saklıyor. Bu yol henüz runtime/test kabulü
almadı; mevcut039 kayıtlarındaki eksik invocation bilgisi geriye doldurulmaz.

Altı parçalı PID/start-ticks/boot/unit/invocation/cgroup ve drain-before-next-cell
şartı [135 kalibrasyon sözleşmesinde](38-care-calibration-execution-review.md)
açıkça tanımlıdır ve korunur. Director owner-generation koşulları da korunur.
Olağan worker’a altı yeni üretim alanı eklemek bu kaynaklarda istenmiyor.
Unit/cgroup drain iddiası ayrıca exact-generation gözlemi gerektirir; PID tek
başına yeterli değildir ve kayıp unit exit0 sayılmaz.

Dondurulmuş243 R3’ün `full_worker_terminal_acceptance` sonucu daha geniş
PID/journal kanıtını da istiyor; bu yardımcı kontrol aynen korunur. Bağımsız
matris bileşeni ayrı raporlanır. Bu yardımcı alan bütün M0 kabul tablosunun
tanımı değildir ve karşılanması tek başına M0’ı tamamlamaz. Kaynak incelemesi
özel audit SHA-256 `b3b814517bb5af8fb926ddfde26318e15ef3026f7ce409c5c8692d4641aa09f7`
ile bağlandı; ana/039/040/041 kaynak envanterleri değişmedi. Sonraki kapı,
hazır041 düzeltmesinin gerçek odaklı test ve PostgreSQL doğrulamasıdır.

## Sonraki kontrollerin çalıştırma bağları

040 PostgreSQL prova R2 için 303 donor dosyası ve altı helper hash’i tekrar
doğrulandı. Ayrı fixture kaynağı henüz yok; port55465 kontrol anında boştu.
Reviewed private ready SHA `af5e1413cd01f474a26a3ec227e18184d1f0c268030c779f2d35ff9c972405c3`.
Bu hazırlık SQL/DB/fixture çalıştırılması değildir. Başlatırken port, RAM/disk,
terminal research ve CPU kilidi yeniden kontrol edilir.

041 focused R2’nin 306 kaynak hash’i ve 172 **beklenen** vaka bağları tekrar
doğrulandı; private ready SHA
`d6f16f75cf2134fcca67355722927c8b002a066b263afc093ad024b5748ebe5a`.
Gerçek test sonucu hâlâ yok. 0035 rehearsal’ın gerçek040 wrapper0/receipt
bağı ise ön koşul gerçekleşmediğinden boş ve yetkilendirilmemiş kaldı.

Aynı kontroldeki aggregate observer `6675c0` gerçek exit0, 0,537 saniye:
**167/243 completed, sıfır failed, üç yöntemde ölçüm**, run running.
Özgün deadline `2026-09-30T04:59:58.870241+00:00` değişmedi. Terminal
journal observer ayrı metadata kontrolü için hazırlandı; ready SHA
`b9e5e7669a7864857ce5d89037d6884e15f63690a75713e5e5b16b94a1e53c9e`.
Henüz çalıştırılmadı; eksik journal exit alanı gelecekte de unknown kalır.
Bu hazır bağlar M0 kabul durumunu veya çalışma kaynağı/image pin’lerini
değiştirmez. Sonuç sonrası public rapor/replay doğrulama aracı ayrıca hazırlanıyor.

## 220 ölçüm ve izleme bağlantısının kapanması

Aggregate observer `b6d9b1`, gerçek exit0 ile **220/243 completed,
sıfır failed, üç yöntem** bildirdi; run hâlâ running. Özgün deadline ve
çalıştırma sözleşmesi değişmedi. API health `ok`, console HTTP200 doğrulandı.

Özgün `systemd-run --wait` terminal bağlantısı (exec session2280) tool
`6c1fa7` ile **exit143**, boş stdout ve devam eden session olmadan kapandı.
Gerçek tool sonucu özel `original-waiter-exit143-tool.private.json` dosyasına
aynen kaydedildi. Bu, araştırma sürecinin exit kodu değildir: ardından
`acf3ed` aynı `6d3a66f1558a424e849ddac7b3edf21d` invocation için active/running
doğruladı. Aktif unit’in ExecMainStatus0 alanı terminal başarı sayılmadı;
araştırma yeniden başlatılmadı. Kalıcı phase sonucu, tamamlanmış ledger ve
terminal metadata daha sonra ayrı doğrulanacak. Eksik dış waiter başarısı
public ölçüm veya rapor/replay doğrulamasıyla karıştırılmayacak.

Sonraki aggregate kontrolü `9dec1e`, gerçek exit0 ile **228/243 completed,
sıfır failed** bildirdi. Rapor/replay R2 kaynak doğrulayıcısı hazırlandı
(SHA `3904bfc6b936e65fbf99534e70242a7fe3f592f215594340659802d77e5b64e7`);
henüz çalıştırılmadı. Tamamlanmış faz ve ledger üzerinden yalnız üretim
raporu/replay eşliğini doğrular; dış süreç ve worker terminal kabulünü
ayrıca false/unverified tutar. R1 ve güçlü135/243 kontrolleri korundu.

## Terminal sonuç ve gerçek sonraki testler

Aggregate `1d8a4a` **243/243 completed, sıfır failed baseline, üç yöntem**
doğruladı; araştırma run state `failed`. Kalıcı araştırma fazı child/phase
exit1 (SHA `d7bc259dfb9fcdf63211b7ac1348f4505f192012472c0b6f059255af3888ed09`).
Exact unit/invocation journal observer `bfee36`, explicit `exited/1`
doğruladı. Önceki waiter143 bu araştırma exit'i değildir.

Kaynak ve özel artifact teşhisi: Director ilk ordinalde `hparam` seçti,
tek preauthored fake fixture `features` döndürdü. Değişmeyen runtime intent
guard öneri preregistration'ından önce `ValueError/provider_error` ile
reddetti; öneri deneyi sayısı sıfır. Başarısız run, rezervasyon ve
artifact'ler korundu; yeniden başlatılmadı. Gelecek fixture gerçek hparam
semantiğine sahip olmalı ve hareket uyumu uzun baseline öncesi doğrulanmalı.

Başarısız run için kapsamı açıkça yalnız baseline metadata olan iki yeni
salt okunur kontrol çalıştırıldı: final checkpoint index `2069b6/exit0`,
243 kayıt ve complete243=true; korumalı completion `eeb8ff/exit0`, 243 kayıt.
Tam score matrix, PID/journal worker terminali, başarılı araştırma,
20 öneri veya yerel LLM kabulü değildir.
[Terminal/baseline özeti](review-evidence/fresh039-terminal-baseline243-summary.json).

041 focused R2 gerçek 172 vakada altı fixture hatasıyla exit1 verdi.
Slice sorgusunu job-state iterator'dan ayıran, timeout keyword'ünü ve
ControlGroup'u doğru taklit eden test düzeltmesi ayrı source-r3 kopyasında
yapıldı; üretim bytes ve mevcut iddialar değişmedi. Gerçek R3 `014294/exit0`:
**172 passed, sıfır failed/error/skipped**, 306 kaynak before/after eş.
[Odaklı test özeti](review-evidence/release041-focused-r3-summary.json).

040 sentetik PostgreSQL R2 gerçek wrapper `562a37/exit1`: bootstrap ve
catalog aşamaları kaydedildi, tamamlanmış scenario sayısı sıfır. Fixture
seed'i sentetik Director unit'i için Scorer'a özel cgroup yardımcısını
çağırıyordu; üretim guard bunu doğru reddetti. Ayrı campaign/DB/port için
R3 fixture düzeltmesi hazırlanıyor; eski DB, kaynak ve hata kanıtı korunur.
Bu sonuç SQL/kurtarma kabulünü tamamlamaz; ana kurulum hâlâ 0.39.0'dır.


## Gerçek sentetik PostgreSQL R3 sonuçları

Ayrı 040 R3 prova **session64849 / `b01aeb` / gerçek dış exit0** ve
receipt exit0 verdi: 99, 180 ve 243 sentetik skor kayıtlı üç senaryo da
ürün CLI exit0 ile `stopped` kapandı. Tamamlanmış pair/blob bytes, özgün
istek bütçesi/deadline ve idempotent recovery korundu; yeni calibration
oluşturulmadı. 0034 upgrade → downgrade → reupgrade catalog eşliği geçti.
Eski R2 seed hatası ve ayrı fixture/DB kanıtı korunur.

0035 R3 **session73089 / `8a054a` / gerçek dış exit0**, receipt exit0:
baseline, primary ve confirmation için üç olumlu READ ONLY RPC ile on
olumsuz SQL kontrolü geçti. Eski OID/ACL/rol/view/trigger/satırlar korundu;
RPC çağrıları satır veya catalog değiştirmedi. [Hashlere bağlı sentetik SQL özeti](review-evidence/release041-synthetic-pg-r3-summary.json).

Bu gerçek PostgreSQL yürütmesinde public ölçüm sayısı **sıfırdır**.
Sentetik process kimlikleri gerçek worker çıkışı değildir; SQL/fixture
kanıtı ana DB clone/promotion, bilimsel araştırma, GPU/LLM/AOS veya M0
kabulünü tamamlamaz. DSN, özel UUID, ham process alanları ve veri dizileri
özete alınmadı.

## Gerçek R4 image ve yedi komutluk kalite kapısı

R3 full gate gerçek dış exit1 (`0fcc30`) verdi: yalnız strict mypy üç
RowMapping/dictionary değişken tipi hatası buldu; pytest 1247 passed idi.
Başarısız gate ve kaynak korunur. Yeni source-r4 içinde yalnız
`stop_closure.py` üç satırla ham satırları ve somut sözlük listesini ayrı
değişkenlerde tutar; SQL, kontrol akışı, test assertion'ları ve strict
mypy şartı değişmedi. Mevcut tek 0.41.0 stamp korundu.

Yeni R4 image gerçek **dış exit0 / `e673fe`**, build/parity/launcher exit0
verdi; user `10001:10001`, kaynak değişikliği yalnız beklenen image pini.
Image digest `sha256:dd00f5b81144103d7d084a08c4e99aceca2bf2f9a7ef0d39810bc22cfe718160`.
Son freeze **306 runtime dosyası + 58 test dokümanı** içeriyor; araç `.venv`
link'i runtime envanterine dahil değil.

R4 full gate **session57927 / `cae391` / gerçek dış exit0**:
Ruff, Pylint, Bandit, pytest, strict mypy, wheel build ve kurulu wheel
import kontrolünün **yedi gerçek çıkış kodu da 0**. Pytest **1247 passed /
7 skipped / 120 deselected**, 33 warning ve 18,51 saniye; GPU/live testleri
hariç. Gate süresi 82,114 saniye, kaynak before/after eş.
[Gerçek image/gate hash özeti](review-evidence/release041-image-gate-r4-summary.json).

Ana kurulum bu kayıt anında hâlâ 0.39.0. Final HEAD/snapshot sonrası ayrı
ana DB clone ve gerçek promotion kapıları bekleniyor; bu image/gate
sonucu gerçek worker terminal, public bilimsel araştırma veya GPU/LLM/AOS
kabulünü tamamlamaz.


## Gerçek tek işlik Scorer process smoke R3

Son R4 kaynak/image ile ayrı sentetik provada gerçek dış wrapper **exit0
(`67bc50`)**, driver exit0 ve **gerçek Scorer waiter exit0** gözlendi.
Normal Director owner/generation, Planner kuyruğu, Scorer claim/evaluate ve
0035 provenance RPC akışı kullanıldı. Tek işin stdout job/unit/invocation
kimliği ve **claim attempt 1**, commit edilmiş RPC metadata'sıyla birebir
eşleşti; normal baseline checkpoint yazma/okuma roundtrip geçti.
Process quiescence ayrıca kontrol edildi. İş 66,961 saniye, toplam prova
68,105 saniye sürdü.

Önceki iki fixture hatası korunur: R1, mevcut olmayan owner-capture
fonksiyonunu import ettiği için SQL/Scorer başlamadan `ImportError` verdi.
R2, Director lease bir bağlantıyı tutarken kayıt akışının ikinci bağlantıya
ihtiyaç duyması nedeniyle `TimeoutError` verdi; gerçek waiter gözlenmedi.
R3 yalnız fixture Director pool'unu iki bağlantı, sıfır overflow ve beş
saniye checkout timeout ile kurdu. Üretim kodu ve diğer rol pool'ları
korundu; eski hata kaynakları, kanıtları, owner generation ve satırlar
silinmedi veya sıfırlanmadı. Başarılı R3'te tüm önceki satırlar ve run
state'leri, catalog/rol/grant ve kaynak bytes korundu.

Bu sonuç **tek gerçek process işi ve provenance taşıma kanıtıdır**.
Girdi sentetiktir; public ölçüm ve model çağrısı sıfır, GPU kullanılmadı.
Araştırma run'ı tamamlanmadı, plan mühürlenmedi ve calibration oluşmadı.
Tam baseline, bilimsel araştırma, public veri, yerel LLM, GPU, AOS birlikte
çalışma veya M0 kabulü tamamlanmış sayılmaz. Özel kimlikler, yollar, DSN ve
ham ölçümler yayımlanacak özete alınmadı.


## Ana 0.41.0 teslimatı ve gerçek clone/promotion

Gerçek ana DB clone R3 **session87574 / `ee5917` / dış exit0** verdi.
0033 → 0034 → 0035, ters migration ve yeniden upgrade; satır/catalog
reversal, gerçek rol/ACL kontrolleri ve kaynak fence'leri geçti. Kaynak
ana DB değişmedi; kendi clone'u durdurulmuş halde korundu.

İlk clone `e75cb6/exit1` yalnız geniş restore stage'inde AssertionError
kaydetti; kesin eski hata satırı bilinmiyor. Cross-DB karşılaştırmasında
276 dahili FK trigger'ın yeni OID'ye bağlı adları kalıyordu. Yeni yardımcı
sadece bilinen builtin adları ve aynı definition adını normalize etti;
trigger alanları, sayıları ve tekrarları korundu. İkinci clone
`08acd4/exit1` karşılaştırma öncesi snapshot'ı sakladı: yalnız 12 açık
soleowner ACL'nin NULL varsayılanına dönüşmesi ve tek view'daki eşdeğer
üç sabitli array cast biçimi farklıydı. R3 kendi clone'unda gerçek owner
olarak aynı yedi owner yetkisini açık ACL'ye geri yazdı; yalnız exact view
ifadesini normalize etti. ACL'ler dışlanmadı; exact clone-içi DDL/reversal,
security ve veri kontrolleri korunarak gerçek başarı alındı. Her iki
başarısız clone, yardımcı ve receipt korunur.

Ana promotion **session86085 / `800c65` / dış exit0**: **13 exact payload**
ve 0033 → 0034 → 0035 uygulandı; runtime artık **0.41.0**, 306 dosya
envanteri ve yukarıdaki `dd00f5b…` imajıyla eşleşiyor. **47 tablodaki
234.073 mevcut satırın tamamı ve hash'leri**, deadline/generation/budget
checkpoint'leri ve beklenen function değişiklikleri dışındaki catalog,
rol ve ACL'ler korundu. Full DB restore kullanılmadı. Yalnız üç sahipli
CPU servisi yeniden başlatıldı; bütçeleri ve tunnel kimliği değişmedi.

Bağımsız sağlık kontrolleri gerçek exit0: API `520502/status ok`, console
`5aeefe/HTTP200`. Overview ve mevcut tamamlanmış mode raporu
`d55dfd/HTTP200`; overview sürümü `2091ca/0.41.0` olarak doğrulandı.
[Redakte edilmiş gerçek teslim özeti](review-evidence/release041-deployment-summary.json).
Tek sentetik gerçek worker kanıtı ayrıca [kendi özetiyle](review-evidence/release041-worker-process-r3-summary.json)
bağlıdır. Bu sürümün kurulması başarısız public run'ı başarılı kılmaz;
tam public research/holdout, yerel LLM, GPU, AOS birlikte çalışma ve M0
kabulleri açık kalır. 0.40 ayrı ana sürüm olarak promote edilmedi; gerekli
stop/provenance düzeltmeleri tek 0.41.0 teslimatında birleştirildi.
