# Gerçek çalışma modu önerisi ve inference içi durdurma

2026-10-01. Arayüz: http://HOST:8788 → Deneyler → koşu → Rapor.

## Geçen: yerel model ile çalışma modu deneyi

Koşu `947fe42f-0bc5-4805-b3b5-716d8c7c5539`, kaynak
`1fbd76f47dc4f30039fa40998ae445df84995f0f`: SQL `completed`, rapor SHA
`59187fc31626df0a74cd205a335a4e7fdb08f1759b480c0f19eb6e4f4dda6781`.
445,523 saniyede 9 başlangıç ve 1 aday bağımsız Scorer ölçümü,
49 SHA doğrulanmış checkpoint tamamlandı. Model `operating-mode-config.v1`
ile LSH parametreleri önerdi; güvenilir host compiler aday kodunu üretti.
Model skor/verdict üretmedi. Aday DISCARD; delta ve CI 0, iyileşme yok.
Holdout kayıtlı değil: manual_review; holdout kabulü sayılmaz.

| Ölçüm | Değer |
|---|---|
| Yerel model / quantization | Qwen/Qwen3.5-9B / fp8_per_tensor |
| Revision | c202236235762e1c871ad0ccb60c8ee5ba337b9a |
| Context / output sınırı / model max | 8192 / 2048 / 10240 token |
| Gerçek input / output | 420 / 155 token |
| Değişmez bütçe | 1 öneri / 2400s / 30000 model token |
| Startup / inference / drain | 77,199s / 8,760s / 0,721s |
| Tepe VRAM / model RAM | 12760 MiB / 10737418240 byte |
| Tahsis | Token14; exact request done; scheduler idle |

Sentetik step snapshot: 192 eğitim, embargo sonrası 86 değerlendirme,
4 sensör. LSH tek mod; destek190, noise2; 67 known ve19 out_of_mode nokta.
86 noktanın OMR değeri mevcut; aralık %0,140809–57,502412. Sensör residual'ları
ayrı; sabit sensörde relative deviation zero_training_range nedeniyle null.
Bu koşu SOM/OPTICS model önerisi, saha başarısı veya sürekli öğrenme değildir.

Model unit inactive/MainPID0; PID448977 ve GPU child449588, cgroup ve UDS yok.
Bağımsız fiziksel readback yalnız KWin12MiB, toplam46MiB. Kuyruk ve AOS devir
gecikmesi ayrı ölçülmedi; toplam süreden türetilmez.

## Geçen: gerçek inference içinde stop ACK ve fiziksel cleanup

Koşu `7038f5fa-da22-4ba1-9f61-237e31f960c4`, aynı kaynak; değişmez bütçe
1 öneri /1800s /30000 token. Exact owner generation1/invocation
`4d49dd9f438d4dac8c70b737cfc044c4`, scheduler token15.
Inference gözleminden sonra stop RPC 8,13ms, HTTP200; tekrar HTTP200.
ACK sonrası hâlâ inference/tahsis vardı. ACK release kanıtı sayılmadı.
Daha sonra exact request done; scheduler idle/next_owner aos;
model PID471600/GPU child472181, cgroup ve UDS yok. VRAM tepe12680MiB;
provider startup/inference/drain receipt yok, bunlar ölçülmüş sunulmaz.
41 checkpoint ve9 baseline Scorer artifact SHA doğrulandı.

## Başarısız / kalan: doğrulanmış terminal kapanış

Dispatch exit0 ve395,603s olmasına rağmen SQL **stop_requested**, report_sha
null; terminal rapor yok. Dispatch recovery_pending/error_type ProgrammingError;
fallback reason: stopped baseline already has frozen calibration.
HTML baseline görünümü terminal rapor yerine geçmez. Kontrol generation1/mode
active kaldı. Fiziksel cleanup geçti, durdurma uçtan uca kabulü geçmedi.
Regresyon-first düzeltme gerekiyor; eski cleanup deadline uzatılmaz ve eski
koşu sonradan başarılı diye işaretlenmez. Yeni sınırlı koşu ayrıca doğrulanmalı.

## Çalıştırılmayan

Gerçek AOS görev → devir → Scientist deney → AOS rapor doğrulama ve adil
birlikte ilerleme. AOS HEAD ed6e857b0e61e9c19c8ba63933e2cc9f318fe444;
son dirty kaynak pinleri source_ready=true/admission=false. İki oturum doğrudan
haberleşiyor; GPU kabulünün tek yürütücüsü Scientist. Eğitim/adaptör yok;
M0 uzun araştırma ve diğer açık kabuller korunuyor. Lisans/CI ayrı işlerdir.


## Dar düzeltme ve yeni gerçek tekrar

İlk regresyon frozen-calibration denial hatasını mevcut kodda yakaladı.
Düzeltme tamamlanmış üç baseline'ın kayıt/cell/calibration/source/champion
kimliğini doğrular; kalibrasyon veya baseline SQL kayıtlarını yeniden yazmaz.
107 ilgili CPU testinin full-path senaryosu fixture'dır, native SQL kabulü değildir.

Yeni gerçek tekrar `7af2df50-cec5-4d07-80bf-8d74af7bdc28`, kaynak HEAD1fbd76f
ve yerel stop_closure SHA8da79d49f9b2088b057a296998a6ba146fd3eb9aa742606fc613d66697fbbc1c:
413,040s/exit0. İlk kalibrasyon engeli aşıldı; sonraki original Scorer finalizer
failed. SQL hâlâ stop_requested/report yok; terminal kabulü **başarısız**.
Eski deadline uzatılmaz ve koşu sonradan başarılı diye işaretlenmez.

ACK11,400ms, canonical token16/request done/scheduler idle. Exact model
PID503363 ve GPU child504055, cgroup ve UDS gone; VRAM tepe12680MiB.
Bu bağımsız fiziksel cleanup terminal raporun yerine geçmez.

Zorunlu kaynak kapısı bu adayda2239 passed/7 skipped/120 deselected/**11 errors**
ile exit1: yeni deadline testinde fixture görünürlüğü problemi. Explicit fixture
import düzeltmesiyle dosyanın15 testi geçti; tam kapı henüz yeniden geçmedi.
AOS karşılıklı review launcher süreç-grubu kimliği ve constructor deadline
bulgularını çıkardı; bunlar düzeltme halinde. Etkin runtime/native AOS kabulü yok.


## Gerçek rol ve eşzamanlı süreç teşhisi

Canlı SQL değiştirilmedi: özel read-only dump yalnız Scientist-owned,384MiB/CPU0,5
izole veritabanına restore edildi. Exact7af kaydı/original artifact yollarında
IndependentScorer ve actual CPU worker kapanışı geçti; SQL yetki değişikliği
gerektiren hata kanıtlanmadı. İki eşzamanlı original supervisor çağrısı aynı
canonical finalizer servisinde RED: exit1/exit0; failed stderr unit already
loaded or has a fragment file. Production logs'taki eşzamanlı API/Director
kapanışlarıyla uyumlu; original failed stderr korunmadığı için eski hatanın
tam stderr'ı geriye dönük varmış gibi sunulmaz. Per-run lifecycle koordinasyonu
ilk absolute deadline ve existing owner/generation kuralları korunarak
düzeltilecek; farklı random unit'lerle paralel finalizer kurulmaz.


## Son gerçek iptal koşusu: terminal kapanış ve fiziksel cleanup geçti

2026-10-01: `0ebf382a-946e-44fc-bdb8-fb8a0c6e432f`, kabul/result HEAD
`1fbd76f47dc4f30039fa40998ae445df84995f0f` ve pinli yerel düzeltmeler.
Canonical finalizer mevcut per-run lifecycle kilidiyle aynı deadline içinde
koordine edildi; farklı bir tahsis otoritesi veya SQL yetki genişletmesi yok.

- Gerçek inference sırasında running → stop_requested → stopped gözlendi.
  İlk ACK 9,519 ms; tekrar HTTP200 ve aynı yanıt. ACK anında aynı owner/request/
  fencing token17 hâlâ GPU kullanıyordu. SQL terminal güncellemesi ACK'ten
  3,080 saniye sonra; toplam koşu427,994 saniye/exit0.
- Generation1, invocation `0ad60acfdb784a14a26488c8ddf2f2e2`; rapor SHA256
  `1c60910efb179c3c77aae540a92bb6afc206df8756d3e521e490e54b208dae2d`.
  Original recovery completed; CLI normal yazıcının stop sonrası ProgrammingError
  bilgisini korur. SQL/API rapor hash'i ayrıca doğrulandı.
- 41 checkpoint ve9 başlangıç Scorer işi hash doğrulamasından geçti;
  kabul edilmiş öneri/aday yok. Bu araştırma başarısı değil, güvenli iptal kanıtıdır.
- Exact request `3083d26e0b05597fb109088cc6f1abdc` done/token17;
  canonical scheduler idle/next_owner=AOS. Model PID541582/GPU child542141,
  orijinal cgroup ve UDS yok; model unit inactive/dead/MainPID0.
  AOS oturumu rapor hash'ini ve aynı fiziksel yoklukları bağımsız doğruladı;
  kendi scheduler DB okuması bulunmadığından idle bilgisini ayrı kanıt saymadı.
- Qwen3.5-9B, fp8_per_tensor, context8192/output2048/model max10240;
  VRAM tepe12680MiB. Launch span79,372s provider startup değildir.
  Startup/inference/drain süreleri ve gerçek iptal token tüketimi bilinmiyor.

**Bütçe kabulü açık:** immutable1 öneri/1800s/30000token sınırında
18432token/180s rezervasyon korunuyor. Yalnız started provider checkpoint var;
terminal bütçe reconciliation veya tamamlanmış/canceled provider receipt yok.
Tam tüketim hesabı doğrulanmadı; rezervasyonu serbest bırakılmış/tüketimi sıfır
göstermiyoruz. Eski7038/7af başarısızlıkları korunur, deadline uzatılmaz.

Son zorunlu kalite kapısı:2259 passed/7 skipped/121 deselected; Ruff, Pylint,
Bandit, pytest, strict mypy, wheel build/import yedi adımın tümü exit0.
Önceki exit1 kapısı tarihsel başarısızlık olarak yukarıda kalır.
Private kanıt özeti SHA256
`43fe9586ed2bbc94c04e1cbebc353a8d93d212c0b256d4de952e4ed573cd5806`;
ham SQL snapshot/DSN/token/veri dosyaları yayına alınmaz.


### Kalıcı konservatif iptal audit'i — kaynak düzeltmesi

Yeni stop recovery yolu original reservation/provider checkpoint hash'lerini
mevcut immutable completed recovery receipt içinde kaydeder. Terminal receipt
yazımı run kilidi altında generation/invocation/execution/report kimliğini
yeniden karşılaştırır; eski işçi yeni koşunun audit'ini yazamaz. Rezervasyonlar
korunur; tüketim bilinmiyorsa actual token/wall null, refund0, conservation=false.
Yeni checkpoint yazma yetkisi, SQL migration veya scheduler yoktur.

38 odaklı kontrol, Ruff, strict mypy ve Pylint exit0. Yeni kaynak gerçek
iptal koşusunda henüz ölçülmedi; önceki0eb receipt'i yeniden yazılmadı.
Finalizer terminal durumunu kaydettikten sonra recovery receipt persist'inden
önce process crash olursa bu audit eksik kalabilir; mevcut terminal retry eski
receipt'i değiştirmez. Dolayısıyla tam crash-window tüketim muhasebesi kabulü
hâlâ açıktır. Bu kaynak iyileştirmesi ölçülmüş token tüketimi veya tam bütçe
reconciliation diye sunulmaz.

Yeni audit kaynak teslimi son mandatory gate:2271 passed/7 skipped/121 deselected;
yedi adımın tümü exit0. Yeni audit gerçek GPU stop koşusunda henüz çalıştırılmadı.
