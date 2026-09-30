# S2 ve gerçek araştırma incelemesi

2026-09-25. Başlangıç kaynak sürümü `92e2bdc`, harness 0.19.0.
Bu dilimin hedefi geçerli gerçek S2 çıktısı ve ardından altı önerilik
araştırma koşusudur. Tanı testi, tam araştırma ve kamu verisi kabulü ayrı
kanıtlar gerektirir.

## Çıktı bütçesi

Önceki birleşik AOS/Qwen tanı çağrısı toplam 512 token sınırında kesildi.
Kurulu vLLM'de düşünme bütçesi ile toplam çıktı sınırı ayrı parametrelerdir;
`finish_reason=length` olan yanıt başarılı kabul edilmez.

Yeni tek çağrılık deneme mevcut `LOCAL_SMOKE_S2_PROFILE` profilini kullanır:
2048 çıktı, 512 düşünme tokenı; temperature 0.6/top_p 0.95;
120 saniye yükleme, 60 saniye çıkarım ve kuyruk dahil 180 saniye toplam.
Üretim profili değiştirilmez. Üretim CandidateProposal şeması ve hareket
eşleşmesi doğrulanır; tanı sırasında aday kodu yürütülmez veya puanlanmaz.
Başarı/hata kaydı yalnız yanıt şekli, token sayıları ve metin uzunluklarını
tutar; düşünme metni kayda alınmaz.

Sınırlama: bu kuyruksuz profilin başarılı olması, AOS bekleme süresi eklenen
birleşik çağrıya 180 saniyenin yeteceğini kanıtlamaz. Daha yüksek scheduler
tavanlarıyla çağrılan farklı profillerde kalıcı lease/worker sınırlarının
profil sınırlarını izlemesi ayrıca incelenecek. Normal araştırma sağlayıcısı
scheduler'ı seçilen profilin sınırlarıyla kurar.

## Altı önerilik araştırma hazırlığı

`local-qwen-019-configuration-review.json`: yeni özel API kimliği, mevcut
dört sentetik EVT görevi, güncel local-Qwen sağlayıcı hash'i ve altı öneri
sınırı doğrulandı. Gerçek PostgreSQL Planner yükleyicisi dört görevi okudu;
manifest/hash ve izinler eşleşti. Hazırlık komutu exit 0.
Bu adım model, run, öneri veya Scorer sonucu üretmedi.

## Tek S2 çağrısının gerçek sonuçları

İki deneme de üretim 0.19.0 kodunu değiştirmeden, aynı 2048/512 profiliyle
yapıldı. Komut `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python
docs/ai-scientist/review-evidence/review_local_qwen_s2_candidate.py --output
<aşağıdaki kanıt yolu>` biçimindedir.

| Kanıt | Gerçek süreç sonucu | Bulgular |
|---|---|---|
| `local-qwen-s2-candidate-019-review.json` | session 50652, exit 1, 157,865 sn | Gözlemci model kapanırken `/proc/<pid>/stat` okumasında yarışa girdi. Peer sonucu yazılamadı; model çıktısı bilinmiyor. Model/üst süreç temizlendi, özel ticket active kaldı. |
| `local-qwen-s2-candidate-019-after-review.json` | session 34672, exit 1, 156,768 sn | Gözlemci düzeltildi; `ModelOutputBudgetExceeded`. 442 giriş/2048 çıkış token, `finish_reason=length`, nihai içerik null. Tek model generation, ticket done, taze cgroup boşluk kontrolü ve GPU temizliği doğrulandı. |

İkinci kayıtta düşünme alanı 3518 karakterdir; metin saklanmadı. Bu sayı tek
başına düşünme token bütçesinin çalışıp çalışmadığını göstermez. Sonucun
kesilmesi geçerli CandidateProposal veya başarılı S2 kabulü değildir.

Gözlemci düzeltmesi yalnız review aracındadır: kapanan PID için en çok bir
saniye yeniden gözlem yapılır; terminal kabulü yeni systemd MainPID=0
gözlemi ister. Kaybolan `/proc` dosyası tek başına boş süreç kanıtı sayılmaz.
Generation değişimi, erişim hatası ve süresinde kararlı hale gelmeyen durum
reddedilir. Altı CPU fixture kontrolü exit 0:
`local-qwen-observer-exit-race-review.json`. Yeni CPU hazırlık kaydı
`local-qwen-s2-cpu-diagnosis-observer-revision.json` da exit 0'dır.

Sonraki tanı aynı üretim çağrısına yalnız vLLM `return_token_ids` yanıt
seçeneğini ekler. Review aracı token dizilerini veya model metnini diske
yazmaz; işaret konumları/sayıları ve tekrar uzunluğu gibi ölçüler tutar.
Bu açık tanı müdahalesi normal üretim koşusu kabulü yerine geçmez.
Kurulu vLLM 0.30.0'ın aktif GPU sampler yolu
`v1/worker/gpu/sample/thinking_budget.py` de kaynak hash'lerine eklenmiştir;
eski `v1/sample/thinking_budget_state.py` okuması tek başına yeterli değildir.

Kaynak kontrolü: [vLLM düşünme çıktısı belgesi](https://docs.vllm.ai/en/latest/features/reasoning_outputs/)
ve [yapılandırılmış çıktı belgesi](https://github.com/vllm-project/vllm/blob/main/docs/features/structured_outputs.md).
Belge desteği bu kurulu model/şema birleşiminin gerçek başarısını kanıtlamaz.

### Token sınırı ve sampler teşhisi

`local-qwen-s2-token-boundary-review.json`: session **69841 / exit 1**,
158,075 sn. Yanıtta 2048 tokenın tamamı reasoning olarak sayıldı;
`</think>` hiç yok, en uzun aynı-token tekrarı **1537**. Prompt tek açık
düşünme başlangıcı içeriyor. Kuyruk/süreç/GPU temizliği geçti. Token dizileri
saklanmadı; yalnız işaret sayıları/konumları ve tekrar ölçüleri tutuldu.

Model yüklemeden kurulu vLLM sampler'ıyla yapılan küçük gerçek GPU deneyi
`vllm-forced-token-sampler-review.json`, session **32880 / exit 1**,
11,334 sn. Worker kendi işlemini exit 0 ile tamamladı; review'un exit 1'i
aşağıdaki başarısız sayısal kontroldür:

| Sabitlenmiş logit | temperature / top_p | top_k | Token korunuyor / doğru seçiliyor |
|---:|---|---:|---|
| 30 | 0.6 / 0.95 | 20 | Evet / evet |
| 1e9 | 0.6 / 0.95 | 20 | Evet / evet |
| 1e9 | 1.0 / 0.95 | 20 | Evet / evet |
| 1e9 | 0.6 / 0.95 | devre dışı | **Hayır / hayır; sıfır sonlu logit, token 0** |
| 1e9 | 0.6 / 1.0 | devre dışı | Evet / evet |
| 1e9 | 0.6 / 1.0 | 20 | Evet / evet |

Bu kontrol yalnız yapay sayısal tensörler kullanır (en yüksek CUDA ayrımı
6.237.184 byte, ayrılmış havuz 27.262.976 byte); model/araştırma kabulü değildir.
Native runtime düşünme bitişini `1e9` logitle zorlayan kurulu V2 sampler'ı
kullanır. Kurulu split top-p yolu `x > pivot_logit` maskesinde büyük logit
için eşitliğe yuvarlanıp tüm tokenları eleyebilir; büyük olmayan değer ve
diğer yollar kontrol olarak ölçülmüştür. Üretim isteğinde `top_k` yoktur.

Pinli model README'si `c202236...` revision'ında hassas kodlama için
0.6/0.95/**top_k=20** önerir (satır 834). Bir sonraki tanı yalnız isteğe
bu sabit ayarı ekler; kurulu vLLM dosyaları değiştirilmez. Bu tanı başarılı
olsa bile üretim profil/receipt/config sözleşmesine uygulanması ve yeniden
doğrulanması gerekir.

`local-qwen-s2-topk20-diagnostic-review.json`: **session 36996 / exit 1**,
154,906 sn. Açık review isteği değişikliği düşünme sınırını düzeltti:
`</think>` konum 511'de bir kere üretildi, reasoning token sayısı 511,
en uzun aynı-token tekrarı 1. Nihai içerik oluştu (7228 karakter), ancak
toplam 2048 token sınırında JSON kesildi. Geçerli öneri kabulü verilmedi.
Ticket done, tek generation, temiz GPU/cgroup ve değişmeyen runtime
kaynakları tekrar doğrulandı. Üretim profilinde açık top_k ve yeterli
fakat sınırlı çıktı/süre bütçesi için Luna uygulaması gerekir. Ham nihai
içerik ve düşünme metni bu review kaydında tutulmadı.

## Paralel uygulama

Kullanıcının hızlandırma talebiyle üç GPT-6 Luna/high ajanı ayrı worktree ve
gerçek PostgreSQL veritabanlarında çalışıyor: dispatcher generation/kurtarma,
immutable report/replay, gerçek public suite ve PDM/NRM Scorer bağlantısı.
Veritabanlarının 0015'e kadar kurulumu üçünde de exit 0:
`parallel-m0-isolated-databases.json`. Kodlama paralel; ağır CPU kontrolleri
ortak kilitle sıraya alınır ve cgroup ile sınırlandırılır. GPU denemelerini
yalnız root koordine eder. Canlı AOS ve ortak bağımlılıklar değiştirilmedi.

## Önceki kesintilerin kalıcı durumu

`local-qwen-historical-recovery-state-review.json` salt okunur PostgreSQL
kontrolüdür; kayıtlar değiştirilmedi:

| Koşu | Durum | Korunan skorlar | Açık durum |
|---|---|---:|---|
| `b54c9282-ca69-4deb-9e28-ef50e36bf21c` | `running` | 44 | Üç baseline/iki öneri scored; plan mühürlenmemiş, rapor yok |
| `75642033-6e46-4a73-82a9-9e1ecb6066d3` | `stop_requested` | 23 | Bir baseline primary_running; plan mühürlenmemiş, rapor yok |

Her iki koşuda kayıtlı Scorer işleri completed. Bu, Director/deney
durumlarının kendiliğinden uzlaştırıldığı anlamına gelmez. Sahiplik ve
checkpoint doğrulayan kalıcı kurtarma açık; SQL'i yapay terminal duruma
çekerek kabul verilmez.

## Kabul sınırı

Review teslimi öncesi ana çalışma ağacının tam kalite kapısı yeniden geçti:
**session 89240 / gerçek exit 0**, yedi komut exit 0, **231 test / 11 hariç**,
strict mypy 69 kaynak. `quality-gate-local-s2-review21.json` tam çıktıyı
saklar; `local-s2-review21-quality-binding.json` test/probe/helper hash'lerini
ve runtime kaynaklarının `92e2bdc` ile değişmemiş olduğunu bağlar.
Paralel worktree değişiklikleri bu kapıya dahil değildir; entegrasyonda
yeni kaynak/imaj/kalite doğrulaması gerekir.

Başarılı S2/tam araştırma sonucu henüz yok. Kamu verisi,
AOS desktop ile tam araştırma, kapasite/eğitim ve önceki koşuların kurtarma
kabulleri açık. Mevcut 22 kapının durumu **3 geçti / 13 kısmi / 6 açık**.
