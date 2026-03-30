from sqlalchemy import create_engine, Engine,Text,event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base,scoped_session
from sqlalchemy import Column, Integer, String, DateTime
from typing import Optional, List
from .models import Feed, Article
from .config import cfg
from core.models.base import Base  
from core.print import print_warning,print_info,print_error,print_success
# 声明基类
# Base = declarative_base()

class Db:
    connection_str: str=None
    def __init__(self,tag:str="默认",User_In_Thread=True):
        self.Session= None
        self.engine = None
        self.User_In_Thread=User_In_Thread
        self.tag=tag
        print_success(f"[{tag}]连接初始化")
        self.init(cfg.get("db"))
    def get_engine(self) -> Engine:
        """Return the SQLAlchemy engine for this database connection."""
        if self.engine is None:
            raise ValueError("Database connection has not been initialized.")
        return self.engine
    def get_session_factory(self):
        return sessionmaker(bind=self.engine, autoflush=True, expire_on_commit=True, future=True)
    def init(self, con_str: str) -> None:
        """Initialize database connection and create tables"""
        try:
            self.connection_str=con_str
            # 检查SQLite数据库文件是否存在
            if con_str.startswith('sqlite:///'):
                import os
                db_path = con_str[10:]  # 去掉'sqlite:///'前缀
                if not os.path.exists(db_path):
                    try:
                        os.makedirs(os.path.dirname(db_path), exist_ok=True)
                    except Exception as e:
                        pass
                    open(db_path, 'w').close()
            self.engine = create_engine(con_str,
                                     pool_size=2,          # 最小空闲连接数
                                     max_overflow=20,      # 允许的最大溢出连接数
                                     pool_timeout=30,      # 获取连接时的超时时间（秒）
                                     echo=False,
                                     pool_recycle=60,  # 连接池回收时间（秒）
                                     isolation_level="AUTOCOMMIT",  # 设置隔离级别
                                    #  isolation_level="READ COMMITTED",  # 设置隔离级别
                                    #  query_cache_size=0,
                                     connect_args={"check_same_thread": False} if con_str.startswith('sqlite:///') else {}
                                     )
            self.session_factory=self.get_session_factory()
            self.ensure_article_columns()
        except Exception as e:
            print(f"Error creating database connection: {e}")
            raise
    def ensure_article_columns(self):
        """Ensure required columns exist for legacy articles tables."""
        try:
            inspector = inspect(self.engine)
            if "articles" not in inspector.get_table_names():
                return

            columns = {column["name"] for column in inspector.get_columns("articles")}
            alter_statements = []
            if "is_favorite" not in columns:
                alter_statements.append("ALTER TABLE articles ADD COLUMN is_favorite INTEGER DEFAULT 0")

            if not alter_statements:
                return

            with self.engine.begin() as conn:
                for stmt in alter_statements:
                    conn.execute(text(stmt))

            print_info(f"[{self.tag}] 文章表结构已自动更新: {', '.join(alter_statements)}")
        except Exception as e:
            print_warning(f"[{self.tag}] 检查/更新 articles 表结构失败: {e}")
    def create_tables(self):
        """Create all tables defined in models"""
        from core.models.base import Base as B # 导入所有模型
        try:
            B.metadata.create_all(self.engine)
        except Exception as e:
            print_error(f"Error creating tables: {e}")

        print('All Tables Created Successfully!')    
        
    def close(self) -> None:
        """Close the database connection"""
        if self.SESSION:
            self.SESSION.close()
            self.SESSION.remove()
            
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    def delete_article(self,article_data:dict)->bool:
        try:
            art = Article(**article_data)
            if art.id:
               art.id=f"{str(art.mp_id)}-{art.id}".replace("MP_WXS_","")
            session=DB.get_session()
            article = session.query(Article).filter(Article.id == art.id).first()
            if article is not None:
                session.delete(article)
                session.commit()
                return True
        except Exception as e:
            print_error(f"delete article:{str(e)}")
            pass      
        return False
     
    def add_article(self, article_data: dict,check_exist=False) -> bool:
        try:
            session=self.get_session()
            from datetime import datetime

            def _to_unix_seconds(value) -> int:
                now_ts = int(datetime.now().timestamp())
                if value is None:
                    return now_ts
                if isinstance(value, datetime):
                    return int(value.timestamp())
                if isinstance(value, (int, float)):
                    iv = int(value)
                    return int(iv / 1000) if iv > 1_000_000_000_000 else iv
                if isinstance(value, str):
                    raw = value.strip()
                    if not raw:
                        return now_ts
                    if raw.isdigit():
                        return _to_unix_seconds(int(raw))
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                        try:
                            return int(datetime.strptime(raw, fmt).timestamp())
                        except ValueError:
                            continue
                    try:
                        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        return now_ts
                return now_ts

            def _to_unix_millis(value, fallback_seconds: int) -> int:
                if value is None:
                    return fallback_seconds * 1000
                if isinstance(value, datetime):
                    return int(value.timestamp() * 1000)
                if isinstance(value, (int, float)):
                    iv = int(value)
                    return iv if iv > 1_000_000_000_000 else iv * 1000
                if isinstance(value, str):
                    raw = value.strip()
                    if not raw:
                        return fallback_seconds * 1000
                    if raw.isdigit():
                        return _to_unix_millis(int(raw), fallback_seconds)
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                        try:
                            return int(datetime.strptime(raw, fmt).timestamp() * 1000)
                        except ValueError:
                            continue
                    try:
                        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
                    except ValueError:
                        return fallback_seconds * 1000
                return fallback_seconds * 1000

            art = Article(**article_data)
            if art.id:
               art.id=f"{str(art.mp_id)}-{art.id}".replace("MP_WXS_","")
            
            if check_exist:
                # 检查文章是否已存在
                existing_article = session.query(Article).filter(Article.url == art.url or Article.id == art.id).first()
                if existing_article is not None:
                    print_warning(f"Article already exists: {art.id}")
                    return False
                
            if art.created_at is None:
                art.created_at=datetime.now()
            if isinstance(art.created_at, str):
                art.created_at=datetime.strptime(art.created_at ,'%Y-%m-%d %H:%M:%S')
            art.updated_at = _to_unix_seconds(art.updated_at)
            art.updated_at_millis = _to_unix_millis(art.updated_at_millis, art.updated_at)
            art.content=art.content

            if art.content_html is None:
                from tools.fix import fix_html
                art.content_html = fix_html(art.content)
            from core.models.base import DATA_STATUS
            art.status=DATA_STATUS.ACTIVE
            session.add(art)
            session.commit()

        except Exception as e:
            session.rollback()
            err_str = str(e).lower()
            if "unique" in err_str or "duplicate" in err_str:
                print_warning(f"Article already exists: {art.id}")
            else:
                print_error(f"Failed to add article: {e}")
            return False
        return True
        
    def get_articles(self, id:str=None, limit:int=30, offset:int=0) -> List[Article]:
        try:
            data = self.get_session().query(Article).limit(limit).offset(offset)
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e    
             
    def get_all_mps(self) -> List[Feed]:
        """Get all Feed records"""
        try:
            return self.get_session().query(Feed).all()
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e
            
    def get_mps_list(self, mp_ids:str) -> List[Feed]:
        try:
            ids=mp_ids.split(',')
            data =  self.get_session().query(Feed).filter(Feed.id.in_(ids)).all()
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e
    def get_mps(self, mp_id:str) -> Optional[Feed]:
        try:
            ids=mp_id.split(',')
            data =  self.get_session().query(Feed).filter_by(id= mp_id).first()
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e

    def get_faker_id(self, mp_id:str):
        data = self.get_mps(mp_id)
        return data.faker_id
    def expire_all(self):
        if self.Session:
            self.Session.expire_all()    
    def bind_event(self,session):
        # Session Events
        @event.listens_for(session, 'before_commit')
        def receive_before_commit(session):
            print("Transaction is about to be committed.")

        @event.listens_for(session, 'after_commit')
        def receive_after_commit(session):
            print("Transaction has been committed.")

        # Connection Events
        @event.listens_for(self.engine, 'connect')
        def connect(dbapi_connection, connection_record):
            print("New database connection established.")

        @event.listens_for(self.engine, 'close')
        def close(dbapi_connection, connection_record):
            print("Database connection closed.")
    def get_session(self):
        """获取新的数据库会话"""
        UseInThread=self.User_In_Thread
        def _session():
            if UseInThread:
                self.Session=scoped_session(self.session_factory)
                # self.Session=self.session_factory
            else:
                self.Session=self.session_factory
            # self.bind_event(self.Session)
            return self.Session
        
        
        if self.Session is None:
            _session()
        
        session = self.Session()
        # session.expire_all()
        # session.expire_on_commit = True  # 确保每次提交后对象过期
        # 检查会话是否已经关闭
        if not session.is_active:
            from core.print import print_info
            print_info(f"[{self.tag}] Session is already closed.")
            _session()
            return self.Session()
        # 检查数据库连接是否已断开
        try:
            from core.models import User
            # 尝试执行一个简单的查询来检查连接状态
            session.query(User.id).count()
        except Exception as e:
            from core.print import print_warning
            print_warning(f"[{self.tag}] Database connection lost: {e}. Reconnecting...")
            self.init(self.connection_str)
            _session()
            return self.Session()
        return session
    def auto_refresh(self):
        # 定义一个事件监听器，在对象更新后自动刷新
        def receive_after_update(mapper, connection, target):
            print(f"Refreshing object: {target}")
        from core.models import MessageTask,Article
        event.listen(Article,'after_update', receive_after_update)
        event.listen(MessageTask,'after_update',receive_after_update)
        
    def session_dependency(self):
        """FastAPI依赖项，用于请求范围的会话管理"""
        session = self.get_session()
        try:
            yield session
        finally:
            session.remove()

# 全局数据库实例
DB = Db(User_In_Thread=True)
DB.init(cfg.get("db"))
