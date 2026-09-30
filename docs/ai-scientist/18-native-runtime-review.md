# Native model runtime — izolasyon ve gerçek Qwen denemeleri

## Güncel sonuç

Gerçek yerel S1 aritmetik doctor çağrısı geçti; S1 tanı profili sıcaklık 0 ve
top_p 1 kullanır. Araştırma S1 profilinin spec'teki 0,7/0,8 ayarlarıyla gerçek
deneyi ayrıca yapılacaktır. S2 düşünme bütçesi çalıştı, fakat 512 tokenlık
yanıt kesildi. Spec S2 örnekleme ayarlarıyla son deneme, başka bir GPU
tüketicisi ortaya çıktığı için gözlemci tarafından durduruldu; başarı iddiası
yoktur. Gerçek AOS birlikte çalışma, altı araştırma deneyi, uzun bağlam ve
QLoRA kapıları açıktır. Aşağıdaki kayıtlar kendi kaynak hash'lerine aittir.

Tarih: 2026-09-25. Bu kayıt, gerçek model lifecycle uygulamasından önce bu
host'ta yapılan sınırlı yetenek ölçümleridir. Qwen, vLLM sunucusu ve AOS modeli
bu iki incelemede çalıştırılmadı. M0.13, M0.14 ve gerçek birlikte çalışma
kabulleri açık kalır.

## İlk gerçek doctor başlatma denemeleri

Üretim `python -m lab.llm.native_runtime --profile s1` yolu, gerçek systemd
principal ve boş özel SQLite DB ile `review_native_doctor.py` tarafından
çağrıldı. Kaynak `native_runtime.py` SHA `69c2ddf97dfc9314f3a7d69edc7bb58fc474a7481f650c01a8b8fe6605308893`
iki denemede de değişmedi. Model yükleme veya yanıt başarısı yoktur.

| Kayıt | Gerçek process sonucu | Gözlem |
|---|---|---|
| `native-qwen-s1-first-review.json` | session 77142, exit 1, 25,003 sn | İç servis başladıktan hemen sonra kapandı; önceden var olan doctor çıktı dizini 0755 olduğu için failure artefaktı da yazılamadı. |
| `native-qwen-s1-diagnostic-review.json` | session 76092, exit 1, 25,075 sn | Yalnız proje doctor çıktı dizini 0700 yapıldıktan sonra aynı kaynak tekrarlandı. Kaydedilen hata: `unit_launch` / `model UDS runtime directory was not created`. |

`native-diagnostic-directory-setup.json` yalnız bu projeye ait çıktı dizininin
UID/tür kontrolünden sonra 0755→0700 değişimini kaydeder. Mevcut dosyalar ve
AOS değiştirilmedi. İkinci denemedeki kalıcı server log'u boştu; worker,
RuntimeDirectory hazır olmadan yapılan kontrolde durduruldu. Başlatma sırası
düzeltmesi gerekir; bu sonuç modelin donanıma sığıp sığmadığını ölçmez.

Her iki supervisor ve model unit sonlandı, model cgroup'u boşaldı, GPU
örnekleri 62 MiB olarak kaldı ve yabancı compute süreci görülmedi. Parent
cgroup'unun 1 GiB peak kaydı ağırlık dosyalarını hashleyen supervisor'a aittir;
model belleği olarak sunulmaz. Wrapper başarılı kabul edilmedi; M0.13 açık.

Başlatma düzeltmesinden sonraki kaynak `bed56044…`, nested service için
`Type=exec` ve sınırlı runtime-directory beklemesi kullandı. Gerçek tekrar
session **8379 / exit 1 / 30,155 sn** ile vLLM komut satırı ayrıştırıcısına
ulaştı: kurulu sürüm `--disable-log-requests` argümanını reddetti.
`native-qwen-s1-exec-review.json` kaynak/komut/ölçüm/temizlik kaydını,
`native-qwen-s1-exec-log.json` hash-doğrulanmış 155 byte hata çıktısını tutar.
Örneklenen worker cgroup peak 641097728 byte, GPU 62 MiB; bu vLLM import/CLI
ölçümüdür, model kapasitesi değildir. Tüm test süreçleri sonlandı.

### Gerçek ağırlık yükleme ve CUDA derleyici yolu hatası

Desteklenmeyen CLI bayrağı çıkarılmış `native_runtime.py` SHA `90a6407b…`
üzerinde `native-qwen-s1-runtime-review.json` gerçek session **40007 / exit 1**
sonucunu kaydeder. Toplam süre **102,166 sn**. vLLM dört safetensors shard'ını
yükledi; ardından FlashInfer örnekleyicisinin hazırlık derlemesi
`Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist`
hatasıyla durdu. Yanıt üretilmedi. Tam 33018 byte log özel dizinde hash'iyle
saklı; son 16000 byte inceleme JSON'una da alınır.

| Ölçüm | Sonuç |
|---|---:|
| Örneklenen en yüksek toplam GPU belleği | 12748 MiB |
| En yüksek GPU sıcaklığı | 56 °C |
| Model cgroup MemoryPeak | 10737418240 byte (10 GiB üst sınır) |
| En düşük host MemAvailable | 19350294528 byte |
| Son gözlenen memory.events | max 46784; oom 0; oom_kill 0 |

RAM sınırında reclaim/baskı gözlendi; RAM kaynaklı süreç öldürme veya CUDA OOM
kanıtı yok. Hata CUDA derleyici yoluna aittir. Shell ortamında `/opt/cuda/bin/nvcc`
ve ayrıca bu projeye ait pinli vLLM ortamındaki
`nvidia/cu13/bin/nvcc` mevcut; servis derleyici yolunu açıkça almalıdır.
Yeni bir sistem paketi kurulmadı. Test unit'leri/cgroup sonlandı ve GPU 62 MiB
tabanına döndü. Bu ölçüm tam S1/S2 veya AOS birlikte kullanım kabulünü kapatmaz.

### Derleyici/başlık uyumsuzluğu ve izole ortam düzeltmesi

CUDA yolu eklenmiş `eb9e5056…` üzerinde gerçek tekrar
`native-qwen-s1-cuda-review.json`: session **99571 / exit 1 / 50,022 sn**.
FlashInfer bu kez NVCC'yi buldu; derleme, vendored CCCL'nin derleyici/başlık
sürüm eşitliği kontrolünde başarısız oldu. Yerel metadata NVCC/CRT/NVVM'nin
**13.4.92**, `cuda.h` ve runtime başlıklarının **13020 (13.2)** olduğunu
gösterdi. Uyumluluk kontrolü devre dışı bırakılmadı.

Bu denemenin log'u ayrıca FlashInfer'ın varsayılan
`/home/cachyos/.cache/flashinfer` yoluna yazdığını gösterdi. Sonraki profile
özel ve sınırlı cache yolları gerekir; mevcut ortak cache silinmedi.
Runtime üst hata mesajı `unadmitted GPU process` oldu; bağımsız gözlemci
yabancı compute süreci görmedi ve log önce compiler crash'ini gösterir.
Sonlanmakta olan model PID'sinin cgroup/GPU tablolarında farklı anlarda
görünmesi olasıdır; bu neden sınıflaması henüz doğrulanmış değildir.
İstek başarısız sayıldı, bütün test süreçleri ve GPU belleği temizlendi.

`cuda132-compiler-wheel-sources.json`, NVIDIA'nın PyPI'deki
[NVCC](https://pypi.org/project/nvidia-cuda-nvcc/13.2.78/),
[CRT](https://pypi.org/project/nvidia-cuda-crt/13.2.78/) ve
[NVVM](https://pypi.org/project/nvidia-nvvm/13.2.78/) **13.2.78** wheel'lerinin
kaynak URL, boyut, SHA-256 ve bağımlılıklarını kaydeder. İndirme/hash kontrolü
session **38604 / exit 0**. Yalnız projenin vLLM ortamındaki bu üç paket
13.4.92→13.2.78 değiştirildi; öncesi/sonrası tam paket listesi karşılaştırıldı.
Kurulum ve `uv pip check` **exit 0**, 196 paket uyumlu:
`cuda132-compiler-install.json`. Sistem CUDA, Torch runtime, AOS ve tarihsel
ortam kaydı değişmedi. Yeni gerçek freeze `vllm-cuda132-requirements.lock`
yerel doğrulanmış wheel kaynaklarını aynen içerir. Bu kurulum sonucu henüz
yeniden derleme veya model yanıtı kanıtı değildir.

### İlk başarılı gerçek S1 çağrısı

`native_runtime.py` SHA **05e2fd8f33a0ce8c2915cceeb0b976c67f97d4dcafdafca1715fb7679c1782f2**,
vLLM 0.30.0, Qwen3.5-9B revision `c202236…`, online `fp8_per_tensor`,
language-only/eager, 4096 bağlam × 1 dizi. Kurulu vLLM'in desteklediği
`VLLM_USE_FLASHINFER_SAMPLER=0` seçildi: `apply_top_k_top_p` + yerel Gumbel
örnekleyicisi. Bu bir model değişikliği değildir; performans yalnız bu ayara
aittir. GCC 16 ile FlashInfer sampler derlemesi uyumsuz olduğundan bu destekli
yol seçildi; derleyici uyumluluk kontrolü atlanmadı.

Cache'ler özel 1 GiB `/tmp` altında, her model unit'ine ait mount içinde
tutulur. Genel `HOME` değişkenini değiştiren ara taslak root tarafından
durduruldu (`native-qwen-home-override-stop.json`); o test model başarısı
sayılmadı. Sonraki başarılı kaynak yalnız cache'e özel değişkenleri ayarlar.
Tarihsel/shared cache silinmedi. `vllm-cuda132-resolved.lock` eski hash lock'un
yalnız üç compiler kaydını günceller; 196 kurulu paket sürümüyle tam eşleşme
`cuda132-resolved-lock-binding.json` içindedir.

Komut:

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_native_doctor.py --profile s1 --output docs/ai-scientist/review-evidence/native-qwen-s1-isolated-review.json
```

Gerçek session **30104 / process exit 0 / 11 kontrol geçti**. Sabit soru
`17 + 25`; model yanıtı: `The sum of 17 and 25 is 42.` Kaynaklar ve ortamın
196 paket sürümü koşu boyunca değişmedi. Fixture model/principal kullanılmadı.

| Ölçüm | Sonuç |
|---|---:|
| Giriş / çıkış token | 46 / 16 |
| Model başlatma | 78,838 sn |
| İstek duvar süresi | 0,621949 sn |
| Çıkış token / istek duvar süresi | yaklaşık 25,7 token/sn |
| Kimlikli model unit/GPU drain | 1,217 sn |
| Dış gözlemcinin toplam koşu süresi (hash kontrolü dahil) | 105,452 sn |
| Örneklenen toplam GPU başlangıç / tepe / bitiş | 62 / 12754 / 62 MiB |
| Model cgroup MemoryPeak | 10 GiB (üst sınır) |
| Son gözlenen memory.events | max 40010; oom 0; oom_kill 0 |
| En düşük host MemAvailable | 19909840896 byte |

İstek süresi ölçümü denetim/transport maliyetini içerir; saf decode hızı veya
uzun bağlam benchmark'ı değildir. Her model çağrısında yeniden yükleme ve
özel cache hazırlığı olduğundan başlangıç maliyeti ayrıca gösterilir. Unit'ler
terminal, model cgroup'u boş, testin GPU PID'leri yok. Bu sonuç AOS birlikte
çalışma, 32k×2 kapasite, altı gerçek deney veya QLoRA kabulünü kapatmaz.

### İlk S2 yanıt kontrolü

Aynı kaynak/env üzerinde düşünme açık ve 128 çıkış token sınırıyla gerçek
S2 çağrısı yapıldı: `native-qwen-s2-isolated-review.json`, session **42237 /
exit 1 / 103,439 sn**. Sunucu hazır oldu ve POST 200 döndü, ancak runtime
`vLLM response has an invalid message body` hatasıyla yanıtı reddetti.
Yanıt token/bitiş metadata'sı tutulmadığından bütçenin düşünme sırasında
tükenmesi henüz kanıtlanmış değildir. Sonraki düzeltme bu metadata'yı
kaydedecek, kesilmiş yanıtı açıkça reddedecek ve sabit S2 doctor sınırını
512 token yapacak. Testin süreçleri/GPU belleği temizlendi; S2 geçti sayılmadı.

## Ağ izolasyonu

Komut:

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_native_network.py --output docs/ai-scientist/review-evidence/native-network-capability-review.json
```

Gerçek process exit **0**, özel ağ alanı kontrolleri **10/10**. Script yalnız
kendi loopback TCP dinleyicisini, özel Unix soketini ve iki sınırlı CPU
systemd servisini kullandı. Harici bir ağ adresine bağlantı denenmedi.

- Kullanıcı systemd servisindeki `IPAddressDeny=any`, kendi host loopback
  dinleyicimize bağlantıyı **engellemedi** (`connect_errno=0`). Bu host'ta bu
  özelliğe tek başına egress garantisi olarak güvenilmeyecek.
- `unshare --user --map-root-user --net` ile farklı bir ağ alanı oluşturuldu.
  Script, alanın host'tan farklı olduğunu doğruladıktan sonra yalnız özel
  alandaki loopback arayüzünü açtı.
- Özel alandan host TCP dinleyicisine bağlantı başarısız oldu; özel alanın
  kendi loopback TCP bağlantısı ve dosya sistemi üzerinden özel Unix soketi
  bağlantısı başarılı oldu. Varsayılan IPv4 rota yoktu.
- Host ağ alanı ve host dinleyicisinin erişilebilirliği korundu; iki test
  servisi de sonlandı. Soket/dizin izinleri sırasıyla 0600/0700 idi.

Kanıt: [native-network-capability-review.json](review-evidence/native-network-capability-review.json).
Bu gözlem production servisinin aynı kısıtlamalarla başladığını veya tüm model
isteklerinin bu sınırdan geçtiğini henüz kanıtlamaz.

vLLM'in API anahtarı bazı API yollarını korur; tek başına bütün servis
yüzeyini kapatmaz. Bu nedenle model servisi özel Unix soketinde tutulacak ve
istekler host denetiminden geçecek. İç iletişim adresi özel loopback olarak
ayarlanacak. [vLLM güvenlik belgesi](https://docs.vllm.ai/en/latest/usage/security/)

## Aynı özel ağ alanında küçük CUDA işlemi

Komut:

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_native_cuda.py --output docs/ai-scientist/review-evidence/native-cuda-capability-review.json
```

Gerçek tool session **33460**, process exit **0**, **14/14** kontrol. Mevcut
vLLM ortamındaki Torch **2.13.0+cu132**, CUDA **13.2**, RTX 4070 Ti SUPER
**sm89** kullanıldı. Model ağırlığı yüklenmedi.

| Ölçüm | Gözlem |
|---|---|
| İşlem | 262144 adet float32 bir, iki ile çarpılıp toplandı; sonuç tam 524288 |
| Tutulan CUDA tensörü | 1048576 byte |
| Torch allocator tepe allocated / reserved | 2098688 / 4194304 byte |
| GPU toplam bellek, başlangıç / örneklenen en yüksek / bitiş | 62 / 318 / 62 MiB |
| Hazır anında okunan cgroup MemoryPeak | 495001600 byte |
| systemd bellek / CPU / swap / tasks sınırı | 2 GiB / 2 CPU / 0 / 64 |
| systemd azami çalışma / durdurma sınırı | 30 / 2 saniye |
| Gerçek worker PID | 2212392; systemd MainPID ve cgroup ile eşleşti |
| Son durum | Worker exit 0, unit inactive/not-found, cgroup boş/yok, GPU PID yok |

Host ön kontrolünde başka compute kullanıcısı yoktu; mevcut login compositor
dar kimlik kontrolüyle istisna tutuldu. Başlatma sırasında dış GPU kullanıcısı
izlendi. Kod yalnız kendi kaydedilmiş systemd generation'ını durdurabilir;
normal başarılı denemede worker özel release işaretini okuyarak sonlandı.
Host ağ alanı değişmedi.

Kanıt: [native-cuda-capability-review.json](review-evidence/native-cuda-capability-review.json).
Örneklenen toplam GPU belleği gerçek tepe değer garantisi değildir; MemoryPeak
hazır anında okundu, sonlanmadan önce son kez okunmuş bütün-koşu tepe değeri
olarak sunulmaz. Bu kontrol scheduler üzerinden çalışmadı; süre aşımı,
zorunlu durdurma, model crash/recovery veya AOS paylaşımı kabulü değildir.

## Sonraki uygulama ve kabul

### Native launcher ve boş aggregate grubun hazırlanması

`native-launch-namespace-capability.json`, planlanan mount ve namespace
ayarlarını zararsız bir CPU child ile çalıştıran tam argv'yi ve çıktıyı tutar.
Process **exit 0**, wrapper kaynak hash'i sabit, unit terminal. Güncellenen
`netns_exec.py`, parent'ın kendi network namespace inode'unu alır; bu host'ta
erişim izni olmayan `/proc/1/ns/net` dosyasını okumaz. Child ayrı network/mount
alanında başladı ve model dizininin mount kaydı `ro` idi. Ağırlık okunmadı;
yalnız config dosyasının varlığı gözlendi.

Gerçek `statvfs('/tmp')` ölçümü bir ayar çakışmasını ortaya çıkardı:

| systemd ayarları | Child `/tmp` kapasitesi | Sonuç |
|---|---:|---|
| `PrivateTmp=yes` + `TemporaryFileSystem=/tmp:rw,size=1073741824` | 16378683392 byte | İstenen 1 GiB sınırı uygulanmadı |
| Yalnız `TemporaryFileSystem=/tmp:rw,size=1073741824` | 1073741824 byte | Ayrı tmpfs, tam 1 GiB |

İlk ölçümün doğrulama script'i **exit 1**, düzeltilmiş seçeneğin script'i
**exit 0** verdi. Child'lar her iki durumda exit 0 ile bitti; veri alanını
doldurma veya model çalıştırma yapılmadı. Kanıtlar:
[ilk ayarlar](review-evidence/native-launch-tmpfs-review.json),
[sınırlı özel tmpfs](review-evidence/native-launch-tmpfs-isolated-review.json).
Üretim launcher'ının aynı düzeltmeyi kullanması ayrıca doğrulanacak.

`swapp-gpu.slice`, önceki CPU yarış denemelerinden kalan, limitsiz ve boş
bir gruptu. `cgroup.procs` boş, `populated 0` ve alt grup olmadığı tekrar
okunduktan sonra bu projeye ait test grubuna açık bir bootstrap uygulandı:

```sh
systemctl --user set-property --runtime swapp-gpu.slice MemoryMax=17179869184 MemorySwapMax=0 CPUQuota=200% TasksMax=128
```

Gerçek **exit 0**, üç kontrol geçti. Kernel `memory.max=17179869184`,
`memory.swap.max=0`, `cpu.max=200000 100000`, `pids.max=128` değerlerini
gösterdi; grup işlemden sonra da boş kaldı. Kanıt:
[native-aggregate-setup-review.json](review-evidence/native-aggregate-setup-review.json).
Yalnız runtime ayarıdır; sistem yeniden başladıktan sonraki kurulum akışının
yerine geçmez. Dolu veya beklenmedik bir grubun sınırları otomatik
değiştirilmeyecek. İlk model worker'ı için 10 GiB bellek üst sınırı ve 6 GiB
host rezervi planlanır; toplam AOS/Lab/sandbox/API/DB beraber kapasitesi bu
boş grup kontrolüyle kabul edilmiş sayılmaz.

### İlk production taslağındaki negatif incelemeler

Kaynak, Luna düzenlemeye devam ederken özel inceleme dizinine byte olarak
kopyalandı; her kayıt çalıştırılan kopyanın hash'ini içerir. Bu sonuçlar
ara taslaklara aittir ve sonraki sürümlerin durumunu tek başına göstermez.

- `review_native_foundation.py`: İlk taslakta gerçek, kısa CPU systemd
  servisini okumak duration alanlarının formatı nedeniyle başarısız oldu;
  aggregate slice adı da unit doğrulamasından geçmedi. Yeni runtime DB'sini
  kurarken bindings tablosunun scheduler'dan önce yaratılması, mevcut legacy
  schema kontrolünü tetikledi. İlk komut **exit 1**:
  [native-foundation-before.json](review-evidence/native-foundation-before.json).
- Genişletilmiş koşu, schema önceden kurulu olduğunda iki ek hatayı ayırdı:
  başarılı launch kaydından sonra process kimliği bağlanamadı; scheduler'ın
  yazma transaction'ı içindeki drain callback ikinci bağlantıdan aynı DB'ye
  yazarken **5,047 saniye sonra `database is locked`** verdi. Bu ara sürümde
  slice ve sonlanmış unit okuması düzeldi, aktif unit duration parser'ında
  eksik `math` import'u kaldı. Altı kontrolden ikisi geçti, **exit 1**:
  [native-foundation-expanded-before.json](review-evidence/native-foundation-expanded-before.json).
  SQL vakalarında gerçek SQLite/scheduler ve açık fixture principal/unit/GPU
  gözlemcileri kullanıldı; model veya GPU çalışmadı.
- `review_native_http_deadline.py`: Özel Unix soketindeki fixture sunucu
  sabit JSON yanıtını 40 ms aralıklarla gönderdi. Native istemciye verilen
  **100 ms** timeout, toplam süreyi sınırlamadı; yanıt **441 ms** sonra kabul
  edildi. Socket timeout tek bir bekleme işlemini sınırlar. Üretim inference
  yolunda toplam duvar saati sınırı ve süresi dolan unit'in durdurulması
  gerekiyor. Gerçek process **exit 1**:
  [native-http-deadline-before.json](review-evidence/native-http-deadline-before.json).

Bu bulgular uygulama ajanına iletildi. Düzeltmelerin güncel kaynak üzerinde
yeniden doğrulanması ve gerçek CPU unit lifecycle incelemesi açık.

Model dosyası ön kontrolü de gerçek yerel klasörde çalıştırıldı. Kaynak
manifestindeki **16 dosyanın tamamı mevcut**, eksik dosya yok. Ancak downloader'ın
`.cache/huggingface` altındaki 35 metadata dosyası, ilk `ModelPin.verify_files`
uygulamasının tam dosya kümesi kontrolünü reddettirdi. Gerçek komut **exit 1**;
SHA/hash taramasına geçmeden dosya kümesi kontrolünde durdu. Kanıt:
[native-model-pin-before.json](review-evidence/native-model-pin-before.json).
Bilinen downloader metadata'sı açıkça ele alınmalı; cache silme veya model
dosyalarının hash kontrolünü kaldırma çözüm olarak kullanılmayacak.

### Temel düzeltmelerin tekrarı ve gerçek CPU unit drain

Native kaynak SHA
`d6cc44287179ef0943384601f830e0d3996d164c548ecf4b0408fa67fbfaa397`
üzerinde iki bağımsız inceleme yeniden çalıştırıldı:

- [Foundation düzeltme kaydı](review-evidence/native-foundation-corrected-review.json):
  **6/6**, gerçek process exit **0**. Aktif ve toplanmış systemd unit
  property'leri okunuyor; yeni DB kuruluyor; created launch kimliği bağlanıyor;
  scheduler release artık SQLite writer kilidine takılmıyor. SQL vakalarında
  principal/unit/GPU fixture sınırı korunur.
- [HTTP deadline düzeltme kaydı](review-evidence/native-http-deadline-corrected-review.json):
  **3/3**, gerçek process exit **0**. Aynı 100 ms yavaş yanıt denemesi artık
  **100,092 ms** sonra hata ile kesiliyor; gecikmiş JSON kabul edilmiyor.
- [Model pin düzeltme kaydı](review-evidence/native-model-pin-corrected-review.json):
  Aynı SHA'daki gerçek `ModelPin.verify_files`, **16 dosyanın 19329393661
  byte'ını** manifest hash'leriyle doğruladı. **13,024 saniye**, gerçek tool
  session **3182**, process exit **0**; CPU servisi 256 MiB RAM, 1 CPU,
  swap 0, 120 saniye üst sınırıyla çalışıp sonlandı. Bilinen downloader
  metadata'sı kabul edildi, model dosyası hash denetimi korundu. Yalnız
  byte/hash okumasıdır; model yükleme veya CUDA başlatma değildir.

Ardından `review_native_unit_drain.py` aynı SHA üzerinde gerçek, sınırlı
systemd CPU servisleriyle çalıştı: **12/14**, gerçek tool session **23432**,
process exit **1**. Principal ve GPU gözlemcisi açık fixture'dır; systemd
InvocationID, MainPID, process başlangıcı, durdurma ve cgroup gözlemi gerçektir.
Model/CUDA yüklenmedi. Kanıt:
[native-unit-drain-before.json](review-evidence/native-unit-drain-before.json).

- Normal scheduler release, kendi unit'ini durdurdu ve cgroup boşaldı.
- Aynı unit adıyla oluşturulan ikinci generation'ın InvocationID'si farklı
  bulundu; eski binding ile kapatma reddedildi ve ikinci süreç korundu.
- Aktivasyonda GPU PID kaydı henüz yazılmamış bir crash durumu fixture ile
  temsil edildi. İlk drain, CPU unit'ini durdurup tutulmaya devam eden GPU
  PID'sini reddetti; **ikinci drain aynı PID hâlâ raporlanırken hatalı olarak
  başarılı döndü**. İlk çağrıdaki yerel PID gözlemi sonraki çağrıda kayboldu.
- Yabancı GPU kullanıcısı fixture'ı devir iznini reddettirdi ve yabancı CPU
  sentinel korundu; ancak **kendi unit'i de açık kaldı**. Hata sonrası kendi
  kaynaklarını durdurma gereksinimi sağlanmadı.

İnceleme sonunda yalnız incelemenin kaydettiği unit generation'ları ve kendi
CPU sentinel'i temizlendi; bütün test unit'leri terminal. Bu iki kalan hata
uygulama ajanına iletildi.

### Düzeltilmiş gerçek CPU unit incelemesi

Aynı script, native kaynak
`62c5a38aeb91107fdadc4e2a4b6590a4b9d4a7a0f489b1f1e8ed2db2cf1fe7bc`
ve scheduler
`fca838d69ba2b4b44db1f849202f4d24d54a2521b4a87d076b8e0193cd44f956`
üzerinde **14/14**, gerçek tool session **23477**, process **exit 0** verdi.
İki kaynak da inceleme boyunca değişmedi. Kanıt:
[native-unit-drain-corrected-review.json](review-evidence/native-unit-drain-corrected-review.json).

Tekrar edilen drain, tutulmaya devam eden fixture GPU PID'sini artık her
seferinde reddediyor. Yabancı GPU PID'si varken kendi unit'i durduruluyor,
yabancı CPU sentinel korunuyor ve kaynak devri reddediliyor. Normal release
ve yeniden kullanılan unit adı/generation kontrolleri de geçti. Bütün test
unit'leri temizlendi. GPU gözlemcisi ve principal fixture sınırı aynen devam
eder; bu sonuç gerçek CUDA model boşaltma kanıtı değildir.

SQLite writer kilidi, callback sırasında kaldırıldı: aktif GPU kaydı kalıcı
olarak tutulurken trusted drain çalışır; son transaction, owner/request/fence
ve geçerli principal veya quarantine durumunu yeniden doğrular. Bu değişimde
ayrıca dar bir kayıt yarışı ölçüldü: aktivasyon drain sırasında karantinaya
alındığında ticket yanlışlıkla `done` yazılabiliyordu; erken yeni admission
olmadı. [Negatif kayıt](review-evidence/native-release-quarantine-ticket-state.json).
Yalnız expiry hesabına `quarantined` kontrolü eklendi; exact diff incelendi.
Yeni scheduler SHA
`2fd153be9ebf430f2c094e37bd2ad4fe0f684c6f50680cd6929ce9ed79b732c0`
üzerindeki hedefli regresyon **1 passed / exit 0**, kaynaklar sabit:
[düzeltme kaydı](review-evidence/native-release-quarantine-corrected-review.json).

`run_turn` artık `enable_thinking: bool` alanını açıkça ister; istek kimliğine
ve `chat_template_kwargs` içine dahil eder. Luna'nın bildirdiği odaklı suite
21 testtir; root'un buradaki bağımsız kanıtları yukarıda belirtilen komut ve
kapsamlarla sınırlıdır. Native taslak için tam kalite kapısı/imaj/commit henüz
yenilenmedi; önceki 177 testli gate, 0.15.0 commit'ine ait kalır.

### Gerçek modelden önce

Luna uygulama ajanının native runtime'ı mevcut GPU scheduler'a bağlanacak:
CPU supervisor ayrı kalacak; model başlatılmadan önce kalıcı unit niyeti
yazılacak; gerçek InvocationID/cgroup kimliği doğrulanacak; aktivasyon,
inference ve toplam süreler sınırlanacak. Devirden önce süreçler ve GPU
kullanımı gerçekten boşalmış olmalı. Unit yeniden kullanımı veya belirsiz
kimlik başka süreci durdurma gerekçesi olmayacak.

Önce production lifecycle CPU kontrolleri ve gerçek unit incelemesi, ardından
çevrimdışı pinli Qwen9B FP8 ile kısa S1/S2 ölçümü yapılacak. 32k × 2 kapasite,
24k QLoRA, AOS model hook'ları, toplam host bütçeleri ve iki uygulamanın gerçek
birlikte ilerlemesi ayrıca açık kabul işleridir.

## S2: 512 token ile ölçülmüş bütçe tükenmesi

Kaynak `40e4ee471ec1cfa6b365293fb19d7c4c544f53b1d1c6599be6d37e619d7bebd6` ile
`review_native_doctor.py --profile s2 --output docs/ai-scientist/review-evidence/native-qwen-s2-bounded-response-review.json`
gerçek tool session **1509 / exit 1** verdi. Kaynaklar ve 196 runtime paketi
değişmedi; paketler yeni CUDA 13.2 resolved lock ile eşleşti. Kanıt:
[native-qwen-s2-bounded-response-review.json](review-evidence/native-qwen-s2-bounded-response-review.json).

HTTP 200 sonrasında kaydedilen metadata: `finish_reason=length`, 38 giriş /
512 çıkış tokenı, `content=null`, reasoning alanı mevcut ve 1807 karakter.
Ham reasoning metni kaydedilmedi. Üretim kodu bu yanıtı
`ModelOutputBudgetExceeded / generation_budget_exhausted` olarak reddetti.
S2 nihai yanıtı **başarılı değildir**; önceki metadata'sız 128 token denemesinin
sebebine dönük geriye dönük kesinlik iddia edilmez.

Toplam gözlem süresi 109,751 sn, GPU tepe 12754 MiB / 64°C, model cgroup peak
10 GiB, minimum host kullanılabilir RAM 19968487424 byte. Teste ait model ve
supervisor unit'leri sonlandı, cgroup boş ve kalan compute GPU kullanıcısı
yok. Bu failure-path temizliği AOS ile birlikte çalışma kabulü değildir.

Sonraki adım, pinli vLLM'nin açık düşünme-token bütçesini inceleyerek mevcut
512 toplam çıktı ve 30 saniye inference sınırı içinde nihai yanıta yer
bırakmaktır. Gerçek S2, altı araştırma deneyi, tam kapasite/eğitim ve AOS
birlikte çalışma açık kalır. Tam 0.16 kalite kapısı/imaj/commit henüz yoktur.

### Açık düşünme bütçesi ile ikinci 512-token denemesi

Kaynak `c131e13117fb5757de40ba8439902b56ed47909827bd726c7d94cf7c72588b64`,
S2 `thinking_token_budget=128`, `max_tokens=512`, inference sınırı 30 sn.
Gerçek tool session **44665 / exit 1**:
[native-qwen-s2-thinking-budget-review.json](review-evidence/native-qwen-s2-thinking-budget-review.json).
Pinli vLLM kaynak incelemesi ayrıca
[native-thinking-budget-source-review.json](review-evidence/native-thinking-budget-source-review.json)
içinde; hiçbir runtime bağımlılığı değiştirilmedi.

Düşünme alanı 454 karakter, nihai içerik alanı 1315 karakter oldu; önceki
1807 karakter düşünme/boş içerik durumundan ayrıldı. Ancak toplam 512 çıkış
tokenında `finish_reason=length` nedeniyle yanıt yine reddedildi. Kısmi nihai
içerik başarılı cevap sayılmadı; ham düşünme/yanıt metni kaydedilmedi.

Gözlem 110,567 sn, GPU tepe 12754 MiB/64°C, model memory peak 10 GiB, host
kullanılabilir RAM en az 19635400704 byte. Bütün sahipli süreçler/cgroup
boşaltıldı ve kalan compute GPU kullanıcısı yok. Kaynaklar ve 196 runtime
paketi değişmedi. S2 kabulü açık.

İncelemede spec §3.10 S2 ayarlarının temperature=0.6/top_p=0.95 olduğu,
mevcut kısa doctor çağrısının ise 0/1 kullandığı görüldü. Bir sonraki değişim
örnekleme parametrelerini açık, doğrulanan ve request kimliğine dahil edilen
alanlar yapıp S2 doctor'ı spec ayarlarına bağlar. Mevcut 512 toplam token,
128 düşünme tokenı ve 30 sn deadline korunur. Bu değişimin yanıtı düzelttiği
gerçek deneme öncesinde iddia edilmez.

### Spec örneklemesi ile deneme: başka GPU tüketicisi nedeniyle durduruldu

Kaynak `8f531e8567c0255f53ef17a508844d4b6306ab859f95cee003897b17df96494b`
S2 için temperature=0.6/top_p=0.95, 512 toplam/128 düşünme tokenı ve 30 sn
inference sınırı kullanır. Gerçek tool session **56456 / exit 1**:
[native-qwen-s2-sampling-profile-review.json](review-evidence/native-qwen-s2-sampling-profile-review.json).

Başlangıçta yalnız izinli display süreci vardı. Gözlemin 103,189. saniyesinde
`session-19.scope` içindeki farklı bir Python süreci GPU kullanmaya başladı.
Bu süreç testin model cgroup'una ait değildi. Gözlemci yalnız kendi
kaydettiği model/supervisor generation'larını durdurdu. Son gözlemde kendi
cgroup'u boş ve süreçleri terminalken diğer süreç GPU'da 3960 MiB kullanmaya
devam ediyordu. Sonraki read-only NVIDIA sorgusunda yine bu dış ortamdan başka
bir Python GPU süreci görüldü; hiçbirine stop/kill gönderilmedi.

Durdurma nedeniyle supervisor'ın systemd-run dönüşü 0 olsa da doctor
sonuç/failure dosyası yoktur. Wrapper'ın gerçek dönüşü **1** ve `passed=false`;
S2 sonucunu veya örnekleme değişiminin etkisini bu denemeden çıkaramayız.
Model memory peak 10607128576 byte, GPU tepe 12754 MiB/61°C. Kaynaklar ve
196 paket değişmedi. Bu olay koordinasyonsuz GPU kullanımının ayrı ele
alınması gerektiğini gösterir; başarılı birlikte çalışma kabulü değildir.
GPU denemeleri şimdilik ertelendi; imaj ve tam CPU kalite kapısı devam ediyor.

## 0.16.0 imaj ve tam kalite kapısı

İlk tam gate gerçek session **86639 / exit 1** verdi: 210 test ve diğer altı
komut geçti, tek hata `netns_exec.py` içindeki shell kullanmayan `os.execv`
çağrısı için B606 uyarısıydı. Çağıran üretim launcher'ın sabit mutlak vLLM
yolunu ve sabit sunucu argümanlarını verdiği incelendi; aday/model çıktısı
komuta girmez. Yalnız bu satıra gerekçeli B606 annotation eklendi. Önce/sonra
Python AST eşliği doğrulandı; runtime davranışı değiştirilmedi.

Final imaj `sha256:a13b86a93300f33dac1bb3325e039d3ab825bba486cc495974b6d2fcf28caf48`,
build ve constrained byte parity **exit 0** (session 95818). Yeni tam gate
gerçek session **8288 / exit 0**: Ruff, Pylint **9.32/10**, Bandit, pytest
**210 passed / 11 deselected**, strict mypy **65 kaynak**, wheel ve izole
import; yedi komutun tamamı 0. Gate sırasında kaynaklar değişmedi.

Harness `0.16.0`, fingerprint
`c7f2c21c4aae1b9551c15447af6cd59c45551208425dbf934e162bb3ab3aa988`
(53 dosya). [Teslim kanıt bağı](review-evidence/native-runtime-quality-gate-binding.json)
imaj, güncel kaynaklar, iki tam gate ve tarihsel gerçek model denemelerinin
hash'lerini açık kapsamlarıyla bağlar. S1 sonucu kendi kaynak/profilinde
tarihsel donanım kanıtıdır; başarısız/yarıda kesilen S2 kayıtları başarısız
kalır. Bu gate gerçek S2, araştırma, kapasite, eğitim veya AOS kabulü değildir.

Git remote salt okunur kontrolde hâlâ boş; [yerel PR taslağı](pr-draft.md)
hazırlandı, PR açılmadı ve otomatik merge yok.
