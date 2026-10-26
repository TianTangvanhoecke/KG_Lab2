import pyodbc, time
import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import datetime as dt

def query_datakap(keys, tagtype):
    database = 'Datakap'
    username = 'ifix' 
    password = 'fix' 
    driver= '{ODBC Driver 17 for SQL Server}'
    server = keys[0]
    location=keys[1]
    part=keys[2]
    description=keys[3]
    start_time=keys[4]
    end_time=keys[5]
    tagid =keys[6]
  #  print('Querying: ',description)
    query = '''SELECT [Time]
                  ,[Description]
                  ,CAST(Value AS NVARCHAR(100)) AS Value
                  ,Location
                  ,Part
                  ,EGU
                  ,TagId
                  FROM [Datakap].[dbo].[v_TagLog] WHERE Location='{}' 
                  AND Part = '{}'
                  AND Description like '%{}%'
                  AND Quality = 192
                  AND Time >= '{}' AND Time < '{}'
                  ORDER BY Time
                  '''.format(location,part,description,start_time,end_time)
    
    query2 = '''SELECT TOP (1) [Time]
                  ,[Description]
                  ,CAST(Value AS NVARCHAR(100)) AS Value
                  ,Location
                  ,Part
                  ,EGU
                  ,TagId
                  FROM [Datakap].[dbo].[v_TagLog]
                  WHERE TagId={} AND Time < '{}' AND Quality = 192
                  ORDER BY Time DESC
                  '''.format(tagid,end_time)
    
    df = pd.DataFrame()
    with pyodbc.connect('DRIVER='+driver+';SERVER='+server+';PORT=1433;DATABASE='+database+';UID='+username+';Pwd='+password+';') as conn:
        import warnings
        warnings.filterwarnings('ignore')
        if tagtype!='String':
            df = pd.read_sql_query(query , conn)
            if df.shape[0]>0:
                df['Value']=pd.to_numeric(df['Value'],errors='coerce')
                df['LocalTime']=df['Time'].dt.tz_localize('UTC').dt.tz_convert('Europe/Berlin').dt.tz_localize(None)
        else:
            df = pd.read_sql_query(query2, conn)
            if df.shape[0]>0:
                df['LocalTime']=df['Time'].dt.tz_localize('UTC').dt.tz_convert('Europe/Berlin').dt.tz_localize(None)      
    return df 



def get_tag_type(tags):
    database = 'Datakap'
    username = 'ifix' 
    password = 'fix' 
    driver= '{ODBC Driver 17 for SQL Server}'
    server = 'beaalai1ac'
    query = '''SELECT [TagId]
                  ,[DataTypeTxt]
                  FROM [Datakap].[dbo].[v_TagsConfig] WHERE [TagId] IN {}
                  '''.format(tags)
    df = pd.DataFrame()
    with pyodbc.connect('DRIVER='+driver+';SERVER='+server+';PORT=1433;DATABASE='+database+';UID='+username+';Pwd='+password+';') as conn:
        import warnings
        warnings.filterwarnings('ignore')
        df = pd.read_sql_query(query , conn)
    return df





                  
