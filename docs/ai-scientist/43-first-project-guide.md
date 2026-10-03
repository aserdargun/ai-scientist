# İlk projeyi arayüzden deneme

> **Güncel 0.46 field-lab:** konsol `127.0.0.1:8789`, API `127.0.0.1:8767`.
> Aşağıdaki varsayılan 8788/8766 adımları eski kuruluma aittir. Güncel
> başlatma ve ilk deney için [teslim kılavuzunu](122-delivery-guide.md) kullanın.

## Güncel Eylemci önizlemesi — 2026-10-03

**Yeni:** geçmiş geliştirme bulguları açık seçimle yeni deney bağlamına taşınabilir.
[0.44.0 kullanım adımları ve tamamlanan örnek](119-prior-findings-context.md).
Son kaynak kalite kapısı 3305 test / yedi komut exit 0; aşağıdaki eski
kapı ve koşu sayıları kendi tarihsel kaynaklarına aittir.

Çalışan ürün önizlemesi `feat/omr-stream-v1` çalışma ağacındadır;
native çekirdek `55c5300` tabanındadır. Ortak AOS başlatıcısının yerel
Scientist değişikliği [koordinasyon kaydındadır](06-aos-coordination.md). Normal kullanıcı terminalinde:

```bash
bash /home/cachyos/ai-scientist/data/runtime/omr-v1/ops/start-lab.sh --profile field-lab
```

Yerel arayüz **http://127.0.0.1:8789**, API **127.0.0.1:8767**.
Eylemci ekranı canlı bağlantıyı, sekiz yeteneği ve gerçek koşu/rapor
geçmişini gösterir. **Deney tasarla** veri, istatistik ve yöntem akışını
açar. Host kapasitesi canlı ölçümden alınır. Öğretmen alanı yerel model,
veri referansı, amaç ve bütçe içeren JSON taslağı indirir; eğitim veya
skill/model otomatik terfisi henüz çalıştırılamaz. Bu profil CPU içindir;
yerel model çağrıları kapalıdır.

**Deney hafızası:** üstteki bağlantıdan
`89dc9a5c-007d-480e-8594-6e14c035a0b9` koşusunu seçin. Üç baseline ve
LSH/OPTICS/SOM için üç deney kaydı, kararları ve doğrulanmış referansları
okunur. Üç yöntem bu sentetik karşılaştırmada DISCARD olmuştur; iyileşme
iddiası yoktur. **Öğretmen veri hazırlığı JSON indir** yalnız referansları
ve eğitim için dışlama nedenlerini kaydeder. Mevcut altı kayıt eğitime
uygun değildir; CPU ölçümleri yerel LLM öğretmen örneği sayılmaz.

**Öğretmen:** yerel öğretmeni, hedef yeteneği ve bütçeyi seçin. Seçili
hafızanın referansları eğitim taslağına eklenir. Bu taslak ve veri hazırlık
manifesti eğitim başlatmaz. Aynı araştırma koşusundaki son 30 geliştirme
geri bildirimi sonraki öneride kullanılabilir; farklı koşular arasında
otomatik yeniden kullanım, gerçek öğretmen işi ve adapter terfisi henüz
tamamlanmamıştır. Durdurulan OMR kaydı ise puansız rapor referansıdır.

Yalnız bu profilin durumunu kontrol etmek ve boşta kapatmak için:

```bash
bash /home/cachyos/ai-scientist/data/runtime/omr-v1/ops/start-lab.sh --profile field-lab --check
bash /home/cachyos/ai-scientist/data/runtime/omr-v1/ops/start-lab.sh --profile field-lab --stop
```

`--stop` aktif iş varken kapanışı reddeder; önce arayüzden ilgili işi
durdurup terminal raporu bekleyin. Otomatik başlangıç/enable varsayılan
değildir; yeniden oturum açınca başlatma komutunu kullanın.

Bu host'taki erişim köprüsünü açmak için:

```bash
systemctl --user start swapp-aserdargun-field-lab-20261003-v1.service
```

**aserdargun cihazından:** http://HOST:8788.
Köprü, yerel 8789 arayüzüne gider; profile ait ayrı erişim kuralı korunur.
Bu profilde 12 ölçümlü CPU grid completed; 64 satır işlenen OMR akışı
stopped raporu verdi. Gerçek boşta kapat/aç sonrası aynı kayıtlar ve
bağımsız raporlar korundu. Bu sonuç GPU/model eğitimi veya gerçek AOS
birlikte çalışma kabulü değildir. İki rapor arayüzde doğru hash ile açıldı.
Artifact yolu düzeltildi; LSH/SOM/OPTICS açıklamalarının her birinde 86 OMR
noktası ve dört sensörün gerçek/NN referans/fark tablosu gerçek kayıttan
görüntülendi. Kaynak kalite kapısı 7/7 adım exit 0 ile geçti
(3244 passed, 7 skipped, 177 deselected); sonraki dar artifact düzeltmesi
17 odaklı test, lint ve bu üç arayüz seçimiyle ayrıca doğrulandı.
[Kısa CPU çalışma kanıtı](review-evidence/field-lab-first-user-workflow-20261003.json).
[Deney hafızası ve öğretmen hazırlığı kanıtı](review-evidence/field-lab-experience-view-20261003.json):
gerçek kayıt okuma, masaüstü/mobil görünüm ve referans indirme doğrulandı.
Yeni hafıza yolu 37 odaklı kontrolden geçti; önceki tam kalite kapısı bu
son değişikliğin tam kapısı olarak sunulmaz. Gerçek öğretmen eğitimi ve
koşular arasında otomatik yeniden kullanım henüz tamamlanmadı.
Aşağıdaki eski örnekler önceki kayıtlı kuruluma aittir.

Yerel arayüz: **http://127.0.0.1:8788**. API ve Director ayrı, kaynakları
sınırlanmış servislerde çalışır. Aşağıdaki grid/baseline örnekleri CPU
kullanır; terminal raporlu yerel model araştırması ayrıca belirtilmiştir.

## Kendiniz başlatma

Bilgisayarı açıp `cachyos` hesabıyla oturum açtıktan sonra terminalde:

```bash
cd /home/cachyos/ai-scientist
bash ops/start-lab.sh
```

Ardından **http://127.0.0.1:8788** adresini açın. Komut mevcut PostgreSQL
konteynerini, API'yi, deney işçisini ve arayüzü sırasıyla hazırlar; zaten
açık olan servisleri yeniden başlatmaz. Terminali kapatabilirsiniz.
Bilgisayarı yeniden açtığınızda aynı komutu çalıştırın.

Bu başlatıcı mevcut yerel kurulumun kayıtlı ayarlarını kullanır. Docker
kapalıysa önce `sudo systemctl start docker` çalıştırın; başlatıcıyı normal
kullanıcı olarak çalıştırın. Veritabanı/kayıtlar yeniden oluşturulmaz.

Servis günlükleri:

```bash
journalctl --user -u swapp-ai-scientist-console.service -n 50 --no-pager
journalctl --user -u swapp-ai-scientist-api.service -n 50 --no-pager
journalctl --user -u swapp-ai-scientist-director-drain.service -n 50 --no-pager
```

## Tamamlanmış yerel model araştırmasını görün

**Deneyler** ekranında `9564b284-d6f8-4297-9f62-1a58383536bd` koşusunu
**İzle → Rapor** ile açın. Gerçek Qwen S1 önerisi,36 baseline ve4 aday
ölçümü, ayrı holdout ve terminal rapor tamamlandı; model işçisi/GPU
kapanışı doğrulandı. Aday **DISCARD** oldu; iyileşme yok, baseline korundu.
Bu tek önerili sentetik koşu AOS birlikte çalışma veya public benchmark
kabulü değildir. [Ölçümler ve kalan işler](105-first-completed-local-research.md).

## Tamamlanmış LSH/OPTICS/SOM projesi

**Deneyler** bölümünde `716a8941-e913-4307-982d-f643e800a216` koşusunun
**Rapor** düğmesini açın. Sentetik `step` verisinde 9 baseline ve üç yöntem
için birer aday olmak üzere **12 gerçek Docker/Scorer ölçümü** tamamlandı.
Yaklaşık 183 saniye sürdü; model token tüketimi sıfırdır.

LSH, OPTICS veya SOM çıktısını seçin. OMR grafiği, mod desteği/toleransı ve
nokta seçicisiyle gerçek değer, NN tahmini, residual ve sensör katkısını
inceleyin. **Rapor JSON indir** tam raporu kaydeder.

- [Saklanan rapor](review-evidence/console0353-completed-mode-report.json)
- [Masaüstü ekranı](review-evidence/console0353-completed-mode-report.png)
- [Mobil ekran](review-evidence/console0353-completed-mode-report-mobile.png)
- [Ölçüm ve tarayıcı kanıtı](review-evidence/operating-console045-completed-project.json)

Bu örnekte komşu sayısı `3`, LSH tablo sayısı `3`, OPTICS `min_samples=6`,
SOM iterasyonu `200`; diğer parametreler varsayılandır. 192 eğitim ve 96
değerlendirme satırından, eğitimle belirlenen 10 satırlık embargo sonrasında
86 satır skorlandı. Tek snapshot sonucu genel benchmark başarısı değildir.

## Kesinti sonrası tamamlanmış 20 önerili proje

**Deneyler** bölümündeki **Koşu kimliği (UUID)** alanına
`c8709872-93db-449a-8aab-13aa0c8c44a2` yazıp **İzle → Rapor** yolunu açın.
Sentetik `step` verisinde LSH seed 0–19 denendi: dokuz baseline ve 20 aday
olmak üzere **29 gerçek Docker/Scorer skoru**, 23 doğrulanmış deney/kayıt çifti
ve sıfır model tokenı. Director ilk öneriden sonra kontrollü kesildi;
ikinci sahiplik nesli özgün bir saatlik bütçeyi koruyarak koşuyu tamamladı.
Devam komutu yaklaşık 326 saniye sürdü.

API, veritabanı ve kanonik rapor eşliği 38 bağımsız kontrolle doğrulandı.
Bu koşu CPU devam akışını gösterir; gerçek model veya public benchmark
sonucu değildir. İlk önerinin bütçesi kesinti öncesinde zaten uzlaştırılmıştı.

- [Tam rapor](review-evidence/resume054-canonical-report.json)
- [Bağımsız doğrulama](review-evidence/resume054-verification.json)

## Durdurulmuş deneyin doğrulanmış raporu

**Koşu kimliği (UUID)** alanına `fac65252-e14d-4014-9744-333b88083963`
yazıp **İzle → Rapor** yolunu açın. Bu koşunun resume denemesi başarısızdı;
0.36.3'te güvenli kapanışla **stopped** durumuna getirildi. Dokuz gerçek
baseline skoru korunur; başlatılmamış öneri **ABANDONED** olarak görünür.
Bu önerinin ölçülmüş skoru veya başarılı araştırma sonucu yoktur.

- [Durdurulmuş koşunun raporu](review-evidence/stop055-canonical-report.json)
- [Kapanışın bağımsız belge doğrulaması](review-evidence/stop055-actual053-closure.json)

## Tamamlanmış gerçek veri projesi

**Deneyler** bölümünde `53424e63-903a-4e48-83a7-7b89eaad0cca` koşusunu
açın. Listede yoksa **Koşu kimliği (UUID)** alanına yapıştırıp **İzle**
düğmesine basın; ardından **Rapor** ve **Rapor JSON indir** kullanılabilir.

Genesis, GECCO, CATSv2 ve SMD üzerinde robust-z, Isolation Forest ve ECOD
algoritmaları üçer seed ile ölçüldü: **36/36 gerçek Docker/Scorer sonucu**,
yaklaşık **11 dakika 11 saniye**, sıfır model çağrısı. API/veritabanı/rapor
hash'i eşleşti; bütçe ve kalibrasyon doğrulandı.

VUS-PR için üç seed ortalamaları aşağıdadır; yüksek değer daha iyidir.

| Seçili kaynak | robust-z | Isolation Forest | ECOD |
|---|---:|---:|---:|
| Genesis | 0,0901 | 0,0083 | 0,0463 |
| GECCO | 0,0318 | 0,1375 | 0,1226 |
| CATSv2 | 0,0316 | 0,0183 | 0,0224 |
| SMD | 0,6515 | 0,5155 | 0,4792 |

Bu sonuçlar seçili üç TSB dosyası ve SMD `machine-1-1` için resmi bölümlerden
12.000'er satır kapsamındadır. Ölçümün tamamlanması her algoritmanın yüksek
başarı gösterdiği anlamına gelmez. Kaynaklar ticari olmayan araştırma
profilindedir; SMD sunucu telemetrisidir.

- [Tam rapor](review-evidence/public050-completed-baseline-report.json)
- [Bağımsız rapor ve yürütme kontrolü](review-evidence/public050-completed-baseline-proof.json)

Tekrar denemek için **Yeni deney başlat → CPU baseline** yolunda
`public-four-source-evt-v1` seçin ve duvar süresi bütçesini **7200 saniye**
yapın. Bu, üst süre sınırıdır; tamamlanan örnek yaklaşık 671 saniye sürdü.

## Tamamlanmış sentetik baseline raporu

**Deneyler** bölümünde `26c9e91f-6db4-4733-bcb9-7ab698b2021b` koşusunun
**Rapor** düğmesini açın. Dört sentetik görevde üç algoritma ve üç seed ile
36 bağımsız ölçüm tamamlandı. **Rapor JSON indir** tam raporu kaydeder.

## Temel istatistik ve PostgreSQL seçimi

1. **Çalışma modları** bölümünü açın.
2. Kaynak olarak `synthetic-db-fixture` seçin. Bu gerçek PostgreSQL'de
   tutulan yerel sentetik veridir. Bağlantı hesabı yalnız izinli görünümü okur.
3. İstediğiniz sensörleri seçin. Eğitim satırlarını `192`, satır sınırını
   `512` bırakın.
4. Başlangıç: `2024-01-01T00:00:00Z`; bitiş: `2024-01-01T00:04:48Z`;
   varlık: `synthetic-machine-01`.
5. **Kaynak snapshot al** düğmesine basın. Eğitim/değerlendirme/tümü
   dönemlerini ayrı inceleyin.
6. Özet tabloda ortalama, standart sapma, minimum, medyan, maksimum ve
   histogram vardır. Sensör ilişkilerinde Pearson/Spearman korelasyonu ve
   kullanılan eşleşme sayısı gösterilir.
7. **Ayrıntılı istatistik ve otokorelasyon** bölümünü açın: yüzdelikler,
   MAD/IQR, varyans, çarpıklık, basıklık, Tukey aykırı değer sayısı, trend ve
   12 gecikmeli ACF görülebilir. Eksik ve sabit değerler ayrıca sayılır.
8. **JSON indir** seçilen dönemin analizini kaydeder.

Bu DB kaynağında güvenilir olay etiketleri olmadığı için Scorer performans
ölçümü kapalıdır. İstatistik analizi kullanılabilir. Eğitim dışındaki
istatistikler öneri sağlayıcısına aktarılmaz.

### Gerçek kamu verisinde istatistik örnekleri

Üretim analiz modülü dört kaynağın seçili eğitim bölümlerinde de çalıştırıldı:
82 sensör, 1.028 Pearson/Spearman sensör çifti; model çağrısı yapılmadı.
Her JSON kaynak/split kimliğini, atfı, dağılımları ve eksik değer sayımlarını
taşır. Bu manifestlerde özgün zaman damgası olmadığı için zaman trendi ve
ACF gerekçeli `null` değerlerdir. UTC içeren DB örneğinde bu analizler arayüzde
görülebilir.

| Kaynak | Eğitim satırı | Sensör | Analiz JSON |
|---|---:|---:|---|
| Genesis | 4.055 | 18 | [İndir](review-evidence/public050-statistics-TSB-AD-M-Genesis.json) |
| GECCO | 16.165 | 9 | [İndir](review-evidence/public050-statistics-TSB-AD-M-GECCO.json) |
| CATSv2 | 16.568 | 17 | [İndir](review-evidence/public050-statistics-TSB-AD-M-CATSv2.json) |
| SMD sunucu telemetrisi | 12.000 | 38 | [İndir](review-evidence/public050-statistics-SMD.json) |

[Gerçek yürütme kaydı](review-evidence/public050-training-statistics-proof.json).

## Sentetik mod ve OMR deneyi

1. Sentetik kaynakta `step` seçin; seed `0` ile **Sentetik snapshot üret**.
2. **Scorer için hazırla** düğmesine basın. Aynı veri daha önce kaydedildiyse
   hazır durumu korunur. Eğitimden türetilen embargo değerlendirme başından
   ayrıca çıkarılır.
3. LSH, OPTICS ve SOM yöntemlerini seçin. Komşu sayısını ve süre bütçesini
   ayarlayın; ilk karşılaştırma için `1800` saniye kullanılabilir.
4. **Yöntem hiperparametreleri, tolerans ve alarm** bölümünde LSH tabloları,
   OPTICS epsilon/xi ve küme boyutları, SOM haritası/iterasyonları, komşu
   ağırlığı, mod toleransı ve alarm parametrelerini ayarlayın.
5. **0 token ile ölçümü başlat**. **Deneyler** sayfasında durumu izleyin.
   Terminal rapor oluştuğunda **Rapor** düğmesi etkinleşir.
6. Ölçülmüş aday çıktısını seçerek OMR zaman dizisini, mod desteği ve
   toleransını, SOM BMU uzaklığını ve sensör katkılarını inceleyin.

OMR yüzdesi olasılık değildir. Bu kısa sentetik çalışma, saha doğruluğu,
uzun süreli yerel model araştırması veya AOS/GPU birlikte çalışma kabulü
anlamına gelmez. Güncel ölçümler ve açık işler
[kabul kaydında](m0-acceptance.md) tutulur.


## Yerel model araştırmasını kendi başınıza hazırlama

Mevcut baseline-proof DB/kimlik kurulumu ve önbellekte yerel model/tokenizer
gereklidir; bu komut boş makine kurucusu değildir. Yeni özel runtime dizini seçin:

```bash
cd /home/cachyos/ai-scientist
.venv/bin/python ops/install_synthetic_research.py \
  --output-runtime /home/cachyos/ai-scientist/data/runtime/my-research \
  --install-holdout --aos-gpu-unit swapp-aos-gpu-joint-acceptance.service
```

Dört sentetik development ve ayrı private holdout hazırlanır. Mevcut kimlikler
korunur; pending konfigürasyon yazılır, model/deney/servis başlatılmaz.
Aktivasyon sadece Scientist servisleri kapalıyken ve güncel owner-bound iş,
Scorer/holdout ve GPU cleanup beklemiyorken kullanılabilir:

```bash
bash ops/start-lab.sh --activate-research
```

Aktif iş varsa komut anlaşılır hata verir; servisleri kendiliğinden durdurmaz.
Şu an açık ana arayüzde model başlatma kapalıdır. Önce pending yapılandırmayı
inceleyin; devam eden kullanıcı işini kesmeyin.

Tamamlanan gerçek model LSH örneği: Deneyler →
947fe42f-0bc5-4805-b3b5-716d8c7c5539 → Rapor.
[Sonuçlar ve stop kabulündeki açık hata](106-mode-agent-and-inflight-stop.md).

## Gerçek SKAB DEV deneyi — 2026-10-03

Gerçek arayüzden SKAB DEV çalışması tamamlandı: 283,41 saniye, 9 baseline +
3 OPTICS skoru; LSH guard reddi. OPTICS KEEP ham VUS-PR üstünlüğü değildir.
OMR/sensör görünümü ve sahipli süreç kapanışı doğrulandı. AOS gerçek GPU,
holdout, öğretmen eğitimi ve otomatik öğrenilmiş iyileşme açık kalır.
[Adımlar ve ölçüm kanıtı](120-public-dev-cpu-study.md).
