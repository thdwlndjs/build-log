-- 데모용 가상 MES 스키마 (Buildlog Phase 2.1 검증 예제, 실제 시스템 아님)
-- dialect: PostgreSQL

CREATE TABLE site (
  id        integer PRIMARY KEY,
  name      varchar(100) NOT NULL,
  biz_no    char(10) NOT NULL
);
COMMENT ON TABLE site IS '사업장(공장)';
COMMENT ON COLUMN site.biz_no IS '사업장 자체의 사업자등록번호, 하이픈 없는 10자리';

CREATE TABLE equipment (
  id            integer PRIMARY KEY,
  site_id       integer NOT NULL REFERENCES site(id),
  name          varchar(100) NOT NULL,
  capacity_kw   numeric(10,2),
  vendor_biz_no char(10)
);
COMMENT ON TABLE equipment IS '설비 마스터. 현재 값만 보관한다';
COMMENT ON COLUMN equipment.vendor_biz_no IS '설비 공급업체의 사업자등록번호';
COMMENT ON COLUMN equipment.capacity_kw IS '설비 정격 용량(kW)';

CREATE TABLE sensor_reading (
  equipment_id  integer NOT NULL,
  reading_time  timestamptz NOT NULL,
  value         double precision,
  operator_code varchar(20),
  PRIMARY KEY (equipment_id, reading_time),
  FOREIGN KEY (equipment_id) REFERENCES equipment(id)
) PARTITION BY RANGE (reading_time);
COMMENT ON TABLE sensor_reading IS '설비 센서 측정값. 1초 단위로 적재되는 시계열';
COMMENT ON COLUMN sensor_reading.operator_code IS '측정 당시 작업자 코드';

CREATE TABLE employee (
  emp_code  varchar(20) PRIMARY KEY,
  name      varchar(100) NOT NULL,
  site_id   integer REFERENCES site(id)
);
COMMENT ON TABLE employee IS '작업자';

CREATE INDEX sensor_reading_time_idx ON sensor_reading (reading_time);

CREATE VIEW daily_equipment_avg AS
  SELECT equipment_id, date_trunc('day', reading_time) AS day, avg(value) AS avg_value
  FROM sensor_reading GROUP BY 1, 2;
