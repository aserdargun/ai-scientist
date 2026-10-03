# Kontrollü AOS → Scientist CPU entegrasyonu

Durum (2026-10-03): izole adayda normal CPU koşusu **completed**; ayrı
kontrollü iptal koşusu **stopped** ve terminal raporu doğrulandı. Gerçek
API, PostgreSQL, Director ve bağımsız Scorer kullanıldı. AOS karar/onay
girdileri fixture idi; otonom model veya web arayüzü kabulü ölçülmedi.
[Giriş ve yetki sözleşmesi](124-aos-cpu-study-contract.md).

## Ölçülen akış

| Alan | Kanıt |
| --- | --- |
| Scientist run | `aa7d3d6f-e44e-4a88-9b53-6fa46f484e0a` |
| Veri | Sentetik çalışma modları: 192 fit / 192 eval; 182 örnek skorlandı |
| Yürütme | `mode-grid`, üç baseline × üç seed + bir OPTICS primary sonucu; 10 task score |
| Karar | OPTICS önerisi `DISCARD`; `KEEP` yok, öğrenilmiş iyileşme kabulü yok |
| Model/GPU | Model tokenı 0; model veya GPU işi başlatılmadı |
| Süre | Director dispatch parent 222.196 s; AOS akışı kuyruk dahil 303.697 s |
| AOS | Kontrollü helper caller exit 0; ilk yanıt ve journal doğrulaması raporu kaydetti |
| Kalite kapısı | Exit 0: 3532 passed / 49 skipped / 177 deselected |

Rapor `single_snapshot_study` ve `benchmark_acceptance=false` taşır. Bu
skorlar sentetik tek snapshot'ın sonuçlarıdır; saha verisi başarısı, bağımsız
benchmark kazanımı veya yöntemlerin genel sıralaması değildir. Raporun
kaydedilmesi onay, eğitim veya saha aksiyonu yetkisi üretmez.

## Sabit kanıt kimlikleri

Özel runtime kanıtları `data/runtime/aos-cpu-acceptance-v1` altında tutulur;
credential, DSN veya ham kişisel günlükler bu belgeye aktarılmaz.

| Artefact | SHA-256 |
| --- | --- |
| Scientist source freeze | `e425b12881cc1c962abf4df7affba262ca8612277efed646e32f1004f663ddd8` |
| Kontrollü CPU scope | `4bfb4c6670c7daded7a284eb24195d3cb2a3f5fcefc9ccbce021524274878af5` |
| Kanonik Scorer raporu | `ef4944202eb341d53b11d9ddf5cd5e181811fea31cc99c5f99665ab07f66068f` |
| `phase-b-report.private.json` zarfı | `979fd3963c3391ec043d6fa02164862d8ef8d14959310f8c70a3591c7b497210` |
| HTML raporu | `3c5414034d441643734b32bee89a85028b3eddefed0ba74c1865055aeb9468f2` |

[İncelenebilir HTML raporu](review-evidence/aos-controlled-cpu-normal-20261003.html).
HTML ayrıca izole adayın `data/runtime/reports/<run_id>.html` yolundadır.
AOS kaynak manifesti: `48fb8530307c5c0397d81c66f535c14a604ddda6ce3e989a913e79e0fa95a827`.
Commit çifti, yerel fark hashleri ve ölçüm sınırları
[makine okunur özette](review-evidence/aos-controlled-cpu-normal-20261003.json) kayıtlıdır.

Rapor görüntüleyicisinin mevcut `Last holdout-approved champion` etiketi bu
koşuda başlangıç baseline referansını gösteriyor. `Holdout status: not_run` ve
`Manual review required: yes` geçerlidir; gerçek holdout onayı yapılmadı. Bu
etiket aday kaynakta `Retained reference candidate` olarak düzeltildi; eski
kanıt HTML’i ve hash’i korunur. Değişiklik canlı field-lab’a dağıtılmadı.
Raporun sekiz mevcut testi ve Ruff geçti; bu kaynak değişikliği önceki
tam kalite kapısı veya dondurulmuş koşu kaynaklarına dahil değildir.

## Kontrollü iptal ve worker temizliği

İptal koşusu `a7c3f7e4-a04c-4a0f-ba7a-2c4a9bea8351`, tekrarlanan iki stop
çağrısında `stop_requested` döndürdü; otomatik recovery sonrasında `stopped`
oldu. Kanonik terminal rapor SHA-256:
`25a95ef4d2fcb6c196408af51fc7ea8800b13b319d6d68e1952c613e0cbc58f7`.

Bu kanıt işlem hattı sınırında stop, tekrar stop ve otomatik recovery akışını
kapsar. SQL kronolojisinde Scorer işi `14:52:36.404943 UTC`'de tamamlandı;
trigger `14:52:36.408127 UTC`, gerçek stop olayı `14:52:36.958495 UTC` idi.
**Scorer'ın stop anında aktif olduğu kanıtlanmadı**; aktif Scorer sırasında
iptal kabulü açık kalır.

`cancel-worker-drain.private.json` SHA-256:
`813a2b14e77092711d0ada4280ff183eb1b8d9a9e06b162b160527a4bca895b3`.
Bu ayrı salt okunur gözlem, üç exact worker unit'inin, kayıtlı süreçlerin ve
cgroup'ların kalmadığını, sandbox kalıntısı olmadığını doğruladı. Normal
koşunun Director, 10 Scorer ve sonlandırıcı süreç/cgroup kapanışı da
doğrulandı. Ardından yalnız bu denemenin API ve PostgreSQL servisleri
kapatıldı: API PID 0, cgroup boş/yok, 8769 kapalı; PostgreSQL exact container
`exited`, PID 0, 55435 kapalı. Son kuyruk 0/0/0; iki terminal koşunun raporları
korundu. Veritabanı volume ve yapılandırması silinmedi. Kapanış kanıtı SHA-256:
`7c6f1a8f1091f0c52f697cc1ffbf8bd0ecfa9acd5f6ad7fcad57fa4517cbc694`.
Canlı field-lab API/console/director-drain sağlıklı kaldı.
[İptal ve kapanış özeti](review-evidence/aos-controlled-cpu-cancellation-20261003.json).

## Açık kabul maddeleri ve sürüm sınırı

- Kontrollü normal koşu ve işlem hattı sınırındaki iptal/tekrar/recovery
  tamamlandı; Scorer aktifken stop kabulü açık.
- Her iki koşunun worker ve izole API/PG kapanışı doğrulandı; kullanıcı
  field-lab servisi çalışmaya devam ediyor.
- Native dışlama, gerçek GPU birlikte çalışma ve release kabulü açık.
- AOS web görevinden typed varlık/veri/yöntem/`field_intent` alanlarının
  gerçek uygulama aktarımı açık; fixture karar/onay bunu tamamlamaz.
- v0.1.0 etiketi ve canlı field-lab 0.46.0 korunur. İzole aday harness
  0.47.0, canlı field profiline dağıtılmadı.

Bu sonuçlar sınırlı CPU entegrasyon kabulüdür. Scorer aktifken durdurma,
gerçek GPU devri ve AOS web uygulamasından uçtan uca kullanım ayrı kabullerdir.
