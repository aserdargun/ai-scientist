# İlk projeyi arayüzden deneme

Yerel arayüz: **http://127.0.0.1:8788**. API ve Director ayrı, kaynakları
sınırlanmış servislerde çalışır. Bu sayfadaki denemeler CPU kullanır ve
yerel dil modeline çağrı yapmaz.

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
